import base64
from datetime import datetime
import json as _json
import hashlib
import logging
import time
from importlib import import_module

from django.db import transaction, DEFAULT_DB_ALIAS

from django.db.utils import IntegrityError

from von_anchor.util import schema_key

from api_v2.models.Issuer import Issuer
from api_v2.models.Schema import Schema
from api_v2.models.Topic import Topic
from api_v2.models.CredentialType import CredentialType
from api_v2.models.Credential import Credential as CredentialModel
from api_v2.models.Claim import Claim

from api_v2.models.Address import Address
from api_v2.models.Attribute import Attribute
from api_v2.models.Name import Name
from api_v2.models.TopicRelationship import TopicRelationship

LOGGER = logging.getLogger(__name__)

PROCESSOR_FUNCTION_BASE_PATH = "api_v2.processor"

SUPPORTED_MODELS_MAPPING = {
    "attribute": Attribute,
    "address": Address,
    "category": Attribute,
    "name": Name,
}


class CredentialException(Exception):
    pass


class Credential(object):
    """A python-idiomatic representation of an indy credential

    Claim values are made available as class members.

    for example:

    ```json
    "postal_code": {
        "raw": "N2L 6P3",
        "encoded": "1062703188233012330691500488799027"
    }
    ```

    becomes:

    ```python
    self.postal_code = "N2L 6P3"
    ```

    on the class object.

    Arguments:
        credential_data {object} -- Valid credential data as sent by an issuer
    """

    def __init__(self, credential_data: object, request_metadata: dict = None, wallet_id: str = None) -> None:
        self._raw = credential_data
        self._schema_id = credential_data["schema_id"]
        self._cred_def_id = credential_data["cred_def_id"]
        self._rev_reg_id = credential_data["rev_reg_id"]
        self._signature = credential_data["signature"]
        self._signature_correctness_proof = credential_data[
            "signature_correctness_proof"
        ]
        self._req_metadata = request_metadata
        self._rev_reg = credential_data["rev_reg"]
        self._wallet_id = wallet_id
        self._witness = credential_data["witness"]

        self._claim_attributes = []

        # Parse claim attributes into array
        # Values are available as class attributes
        claim_data = credential_data["values"]
        for claim_attribute in claim_data:
            self._claim_attributes.append(claim_attribute)

    def __getattr__(self, name: str):
        """Make claim values accessible on class instance"""
        try:
            claim_value = self.raw["values"][name]["raw"]
            return claim_value
        except KeyError:
            raise AttributeError(
                "'Credential' object has no attribute '{}'".format(name)
            )

    @property
    def raw(self) -> dict:
        """Accessor for raw credential data

        Returns:
            dict -- Python dict representation of raw credential data
        """
        return self._raw

    @property
    def json(self) -> str:
        """Accessor for json credential data

        Returns:
            str -- JSON representation of raw credential data
        """
        return _json.dumps(self._raw)

    @property
    def schema_origin_did(self) -> str:
        """Accessor for schema origin did

        Returns:
            str -- origin did
        """
        return schema_key(self._schema_id).origin_did

    @property
    def origin_did(self) -> str:
        """Accessor for the cred def origin did

        Returns:
            str -- origin did
        """
        if self._cred_def_id:
            key = self._cred_def_id.split(':')
            return key[0]
        return None

    @property
    def schema_name(self) -> str:
        """Accessor for schema name

        Returns:
            str -- schema name
        """
        return schema_key(self._schema_id).name

    @property
    def schema_version(self) -> str:
        """Accessor for schema version

        Returns:
            str -- schema version
        """
        return schema_key(self._schema_id).version

    @property
    def claim_attributes(self) -> list:
        """Accessor for claim attributes

        Returns:
            list -- claim attributes
        """
        return self._claim_attributes

    @property
    def cred_def_id(self) -> str:
        """Accessor for credential definition ID

        Returns:
            str -- the cred def ID
        """
        return self._cred_def_id

    @property
    def request_metadata(self) -> dict:
        return self._request_metadata

    @property
    def wallet_id(self) -> str:
        """Accessor for credential wallet ID, after storage

        Returns:
            str -- the wallet ID of the credential
        """
        return self._wallet_id

    @wallet_id.setter
    def wallet_id(self, val: str):
        self._wallet_id = val


class CredentialClaims:
    def __init__(self, cred: CredentialModel):
        self._cred = cred
        self._claims = {}
        self._load_claims()

    def _load_claims(self):
        claims = getattr(self._cred, '_claims_cache', None)
        if claims is None:
            claims = {}
            for claim in self._cred.claims.all():
                claims[claim.name] = claim.value
            setattr(self._cred, '_claims_cache', claims)
        self._claims = claims

    def __getattr__(self, name: str):
        """Make claim values accessible on class instance"""
        try:
            return self._claims[name]
        except KeyError:
            raise AttributeError(
                "'Credential' object has no attribute '{}'".format(name)
            )


class CredentialManager(object):
    """
    Handles processing of incoming credentials. Populates application
    database based on rules provided by issuer are registration.
    """

    def __init__(self) -> None:
        self._cred_type_cache = {}

    @classmethod
    def get_claims(cls, credential):
        if isinstance(credential, Credential):
            return credential
        elif isinstance(credential, CredentialModel):
            return CredentialClaims(credential)

    @classmethod
    def process_mapping(cls, rules, credential):
        """
        Takes our mapping rules and returns a value from credential
        """
        if not rules:
            return None

        # Get required values from config
        try:
            _input = rules["input"]
            _from = rules["from"]
        except KeyError as error:
            raise CredentialException(
                "Every mapping must specify 'input' and 'from' values."
            )

        # Processor is optional
        processor = rules.get("processor")

        claims = cls.get_claims(credential)

        # Get model field value from string literal or claim value
        if _from == "value":
            mapped_value = _input
        elif _from == "claim":
            try:
                mapped_value = getattr(claims, _input)
            except AttributeError as error:
                raise CredentialException(
                    "Credential does not contain the configured claim '{}'".format(
                        _input
                    )
                )
        else:
            raise CredentialException(
                "Supported field from values are 'value' and 'claim'"
                + " but received '{}'".format(_from)
            )

        # If we have a processor config, build pipeline of functions
        # and run field value through pipeline
        if processor is not None:
            pipeline = []
            # Construct pipeline by dot notation. Last token is the
            # function name and all preceeding dots denote path of
            # module starting from `PROCESSOR_FUNCTION_BASE_PATH``
            for function_path_with_name in processor:
                function_path, function_name = function_path_with_name.rsplit(".", 1)

                # Does the file exist?
                try:
                    function_module = import_module(
                        "{}.{}".format(PROCESSOR_FUNCTION_BASE_PATH, function_path)
                    )
                except ModuleNotFoundError as error:
                    raise CredentialException(
                        "No processor module named '{}'".format(function_path)
                    )

                # Does the function exist?
                try:
                    function = getattr(function_module, function_name)
                except AttributeError as error:
                    raise CredentialException(
                        "Module '{}' has no function '{}'.".format(
                            function_path, function_name
                        )
                    )

                # Build up a list of functions to call
                pipeline.append(function)

            # We want to run the pipeline in logical order
            pipeline.reverse()

            # Run pipeline
            while len(pipeline) > 0:
                function = pipeline.pop()
                mapped_value = function(mapped_value)

        return mapped_value

    def get_credential_type(self, credential: (Credential, CredentialModel)):
        """
        Fetch the credential type for the incoming credential
        """
        LOGGER.debug(">>> get credential context")
        start_time = time.perf_counter()
        result = None
        type_id = getattr(credential, 'credential_type_id', None)
        if type_id:
            result = self._cred_type_cache.get(type_id)
            if not result:
                result = CredentialType.objects.get(pk=type_id)
                self._cred_type_cache[type_id] = result
        elif isinstance(credential, Credential):
            cache_key = (credential.cred_def_id,)
            result = self._cred_type_cache.get(cache_key)
            if not result:
                try:
                    issuer = Issuer.objects.get(did=credential.origin_did)
                    schema = Schema.objects.get(
                        origin_did=credential.schema_origin_did,
                        name=credential.schema_name,
                        version=credential.schema_version,
                    )
                except Issuer.DoesNotExist:
                    raise CredentialException(
                        "Issuer with did '{}' does not exist.".format(
                            credential.origin_did
                        )
                    )
                except Schema.DoesNotExist:
                    raise CredentialException(
                        "Schema with origin_did"
                        + " '{}', name '{}', and version '{}' ".format(
                            credential.schema_origin_did,
                            credential.schema_name,
                            credential.schema_version,
                        )
                        + " does not exist."
                    )

                result = CredentialType.objects.get(schema=schema, issuer=issuer)
                self._cred_type_cache[cache_key] = result
        LOGGER.debug(
            "<<< get credential context: " + str(time.perf_counter() - start_time)
        )
        if not result:
            raise CredentialException("Credential type not found")
        return result

    def process(self, credential: Credential, check_from_did: str = None) -> CredentialModel:
        """
        Processes incoming credential data and returns related Topic

        Returns:
            Credential -- the processed database credential
        """
        if check_from_did and check_from_did != credential.origin_did:
            raise CredentialException(
                "Credential origin DID '{}' does not match request origin DID '{}'".format(
                credential.origin_did, check_from_did))
        credential_type = self.get_credential_type(credential)

        with transaction.atomic():
            return self.populate_application_database(credential_type, credential)

    def reprocess(self, credential: CredentialModel):
        """
        Reprocesses an existing credential in order to update the related search models
        """
        credential_type = self.get_credential_type(credential)
        processor_config = credential_type.processor_config

        with transaction.atomic():
            self.remove_search_models(credential)
            self.create_search_models(credential, processor_config)

    @classmethod
    def resolve_credential_topics(cls, credential, processor_config) -> (Topic, Topic):
        """
        Resolve the related topic(s) for a credential based on the processor config
        """
        topic_defs = processor_config["topic"]
        # We accept object or array for topic def
        if type(topic_defs) is dict:
            topic_defs = [topic_defs]

        result = (None, None)

        # Issuer can register multiple topic selectors to fall back on
        # We use the first valid topic and related parent if applicable
        for topic_def in topic_defs:
            related_topic = None
            topic = None

            related_topic_name = cls.process_mapping(
                topic_def.get("related_name"), credential
            )
            related_topic_source_id = cls.process_mapping(
                topic_def.get("related_source_id"), credential
            )
            related_topic_type = cls.process_mapping(
                topic_def.get("related_type"), credential
            )

            topic_name = cls.process_mapping(
                topic_def.get("name"), credential
            )
            topic_source_id = cls.process_mapping(
                topic_def.get("source_id"), credential
            )
            topic_type = cls.process_mapping(
                topic_def.get("type"), credential
            )

            # Get parent topic if possible
            if related_topic_name:
                try:
                    related_topic = Topic.objects.get(
                        credentials__names__text=related_topic_name
                    )
                except Topic.DoesNotExist:
                    continue
            elif related_topic_source_id and related_topic_type:
                try:
                    related_topic = Topic.objects.get(
                        source_id=related_topic_source_id, type=related_topic_type
                    )
                except Topic.DoesNotExist:
                    pass

            # Current topic if possible
            if topic_name:
                try:
                    topic = Topic.objects.get(credentials__names__text=topic_name)
                except Topic.DoesNotExist:
                    continue
            elif topic_source_id and topic_type:
                # Special Case:
                # Create a new topic if our query comes up empty
                try:
                    topic = Topic.objects.get(
                        source_id=topic_source_id, type=topic_type
                    )
                except Topic.DoesNotExist as error:
                    topic = Topic.objects.create(
                        source_id=topic_source_id, type=topic_type
                    )

            # We stick with the first topic that we resolve
            if topic:
                if not related_topic and related_topic_source_id and related_topic_type:
                    related_topic = Topic.objects.create(
                        source_id=related_topic_source_id, type=related_topic_type
                    )
                result = (topic, related_topic)
        return result

    @classmethod
    def credential_cardinality(cls, credential, processor_config):
        """
        Extract the credential cardinality values and hash
        """
        fields = processor_config.get("cardinality_fields") or []
        values = {}
        for field in fields:
            try:
                values[field] = getattr(credential, field)
            except AttributeError as error:
                raise CredentialException(
                    "Issuer configuration specifies field '{}' in cardinality_fields "
                    "value does not exist in credential. Values are: {}".format(
                        field, ", ".join(list(credential.claim_attributes))
                    )
                )
        if values:
            hash_fields = ["{}::{}".format(k, values[k]) for k in values]
            hash = base64.b64encode(
                hashlib.sha256(",".join(hash_fields).encode("utf-8")).digest()
            )
            return {"values": values, "hash": hash}
        return None

    @classmethod
    def process_credential_properties(cls, credential, processor_config) -> dict:
        """
        Generate a dictionary of additional credential properties from the processor config
        """
        config = processor_config.get("credential")
        args = {}
        if config:
            effective_date = cls.process_mapping(
                config.get("effective_date"), credential
            )
            if effective_date:
                try:
                    # effective_date could be seconds since epoch
                    effective_date = datetime.utcfromtimestamp(
                        int(effective_date)
                    ).isoformat()
                except ValueError:
                    # If it's not an int, assume it's already ISO8601 string.
                    # Fail later if it isn't
                    pass
                args["effective_date"] = effective_date

            revoked = cls.process_mapping(
                config.get("revoked"), credential
            )
            if revoked:
                args["revoked"] = bool(revoked)

            inactive = cls.process_mapping(
                config.get("inactive"), credential
            )
            if inactive:
                args["inactive"] = bool(inactive)
        return args

    @classmethod
    def create_search_models(cls, credential: CredentialModel, processor_config,
                             search_model_map=None, save=True):
        """
        Create search model instances using mapping from issuer config

        Returns: a list of the unsaved model instances
        """
        mapping = processor_config.get("mapping") or []
        if search_model_map is None:
            search_model_map = SUPPORTED_MODELS_MAPPING
        result = []

        for model_mapper in mapping:
            model_name = model_mapper["model"]

            try:
                Model = search_model_map[model_name]
                model = Model()
            except KeyError as error:
                raise CredentialException(
                    "Unsupported model type '{}'".format(model_name)
                )

            for field, field_mapper in model_mapper["fields"].items():
                setattr(
                    model,
                    field,
                    cls.process_mapping(field_mapper, credential),
                )
            if model_name == "category":
                model.format = "category"

            # skip blank in names and attributes
            if model_name == "name" and (model.text is None or model.text is ""):
                continue
            if (model_name == "category" or model_name == "attribute") and \
                    (not model.type or model.value is None or model.value is ""):
                continue

            model.credential = credential
            if save:
                model.save()
            result.append(model)
        return result

    @classmethod
    def remove_search_models(cls, credential: CredentialModel, search_model_map=None,
                             raw_delete=True):
        """
        Delete any existing search model instances
        """
        if search_model_map is None:
            search_model_map = SUPPORTED_MODELS_MAPPING
        for model_key, model_cls in search_model_map.items():
            if model_key == "category":
                continue
            rows = model_cls.objects.filter(credential=credential)
            if raw_delete:
                # Don't trigger search reindex (yet)
                rows._raw_delete(using=DEFAULT_DB_ALIAS)
            else:
                rows.delete()

    @classmethod
    def revoke_previous_credentials(cls, topic, credential_type, cardinality=None):
        # We search for existing credentials by cardinality fields
        # to revoke credentials occuring before latest credential
        existing_credential_query = {
            "cardinality_hash": cardinality["hash"] if cardinality else None,
            "credential_type": credential_type,
            "revoked": False,
            "topic": topic,
        }
        existing_credentials = CredentialModel.objects.filter(
            **existing_credential_query
        )
        if cardinality:
            existing_credentials = existing_credentials.prefetch_related("claims")

        latest = existing_credentials.latest("effective_date")
        for existing_credential in existing_credentials:
            if (
                existing_credential.effective_date > latest.effective_date
                or existing_credential == latest
            ):
                continue
            if cardinality:
                # we already checked the hash,
                # but check the claim values just to be sure
                existing_claims = {}
                for claim in existing_credential.claims.all():
                    if claim.name in cardinality["values"]:
                        existing_claims[claim.name] = claim.value
                if existing_claims != cardinality["values"]:
                    LOGGER.warn("Credential hash matched but cardinality claims do not")
                    continue
            existing_credential.revoked = True
            existing_credential.save()

    @classmethod
    def populate_application_database(cls, credential_type: CredentialType,
                                      credential: Credential) -> CredentialModel:
        LOGGER.warn(">>> store cred in local database")
        start_time = time.perf_counter()
        processor_config = credential_type.processor_config

        topic, related_topic = cls.resolve_credential_topics(credential, processor_config)

        # If we couldn't resolve _any_ topics from the configuration,
        # we can't continue
        if not topic:
            raise CredentialException(
                "Issuer registration 'topic' must specify at least one valid topic name "
                "OR topic type and topic source_id"
            )

        cardinality = cls.credential_cardinality(
            credential, processor_config
        )

        # We always create a new credential model to represent the current credential
        # The issuer may specify an effective date from a claim. Otherwise, defaults to now.

        credential_args = {
            "cardinality_hash": cardinality["hash"] if cardinality else None,
            "credential_def_id": credential.cred_def_id,
            "credential_type": credential_type,
            "wallet_id": credential.wallet_id,
        }
        credential_args.update(
            cls.process_credential_properties(credential, processor_config)
        )

        dbCredential = topic.credentials.create(**credential_args)

        # Create and associate claims for this credential
        for claim_attribute in credential.claim_attributes:
            claim_value = getattr(credential, claim_attribute)
            Claim.objects.create(
                credential=dbCredential, name=claim_attribute, value=claim_value
            )

        # Create topic relationship if needed
        if related_topic is not None:
            try:
                TopicRelationship.objects.create(
                    credential=dbCredential, topic=topic, related_topic=related_topic
                )
            except IntegrityError:
                raise CredentialException(
                    "Relationship between topics '{}' and '{}' already exist.".format(
                        topic.id, related_topic.id
                    )
                )

        # Revoke previous credentials in the credential set
        cls.revoke_previous_credentials(topic, credential_type, cardinality)

        # Save search models
        cls.create_search_models(dbCredential, processor_config)

        LOGGER.warn(
            "<<< store cred in local database: " + str(time.perf_counter() - start_time)
        )

        return dbCredential

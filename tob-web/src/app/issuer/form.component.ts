import { Component, OnInit, OnDestroy } from '@angular/core';
import { ActivatedRoute } from '@angular/router';
import { Fetch, Model } from '../data-types';
import { GeneralDataService } from '../general-data.service';
import { Subscription } from 'rxjs/Subscription';

@Component({
  selector: 'issuer-form',
  templateUrl: '../../themes/_active/issuer/form.component.html',
  styleUrls: ['../../themes/_active/issuer/form.component.scss']
})
export class IssuerFormComponent implements OnInit, OnDestroy {
  id: number;

  private _loader = new Fetch.ModelLoader(Model.Issuer);
  private _credTypes = new Fetch.ModelListLoader(Model.IssuerCredentialType);
  private _idSub: Subscription;

  constructor(
    private _dataService: GeneralDataService,
    private _route: ActivatedRoute,
  ) { }

  ngOnInit() {
    this._loader.ready.subscribe(result => {
      this._dataService.loadList(this._credTypes, {parentId: this.id});
    });
    this._idSub = this._route.params.subscribe(params => {
      this.id = +params['issuerId'];
      this._dataService.loadRecord(this._loader, this.id);
    });
  }

  ngOnDestroy() {
    this._idSub.unsubscribe();
    this._loader.complete();
    this._credTypes.complete();
  }

  get result() {
    return this._loader.result;
  }

  get result$() {
    return this._loader.stream;
  }

  get credTypes$() {
    return this._credTypes.stream;
  }
}

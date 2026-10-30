import { Component, computed, effect, inject, input, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { Router, RouterLink } from '@angular/router';
import { MatButtonModule } from '@angular/material/button';
import { MatCardModule } from '@angular/material/card';
import { MatChipsModule } from '@angular/material/chips';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatIconModule } from '@angular/material/icon';
import { MatInputModule } from '@angular/material/input';
import { MatPaginatorModule, PageEvent } from '@angular/material/paginator';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { MatSelectModule } from '@angular/material/select';
import { MatSortModule, Sort } from '@angular/material/sort';
import { MatTableModule } from '@angular/material/table';
import { Subject, debounceTime } from 'rxjs';
import { ApiService } from '../core/api.service';
import { AuthService } from '../core/auth.service';
import { display, isGeo } from '../core/format';
import { problemMessage } from '../core/http';
import { FieldSpec, Row } from '../core/spec.types';
import { ViewState, ViewStateComponent } from '../shared/view-state.component';

/** Список записей любой сущности: поиск, фильтры, сортировка, пагинация, права роли. */
@Component({
  selector: 'app-entity-list',
  imports: [FormsModule, RouterLink, MatTableModule, MatPaginatorModule, MatSortModule, MatFormFieldModule, MatInputModule, MatSelectModule, MatButtonModule, MatIconModule, MatCardModule, MatChipsModule, MatProgressBarModule, ViewStateComponent],
  template: `
    <div class="page-header">
      <div>
        <h1>{{ spec().titlePlural }}</h1>
        @if (spec().description) { <p class="muted">{{ spec().description }}</p> }
      </div>
      @if (canCreate()) {
        <a mat-flat-button [routerLink]="['/e', spec().route, 'new']"><mat-icon>add</mat-icon>Создать</a>
      }
    </div>
    <mat-card appearance="outlined">
      <div class="toolbar-row">
        <mat-form-field appearance="outline" class="search">
          <mat-label>Поиск</mat-label>
          <mat-icon matPrefix>search</mat-icon>
          <input matInput [ngModel]="q()" (ngModelChange)="search$.next($event)" placeholder="Введите текст" />
        </mat-form-field>
        @for (f of filterFields(); track f.name) {
          <mat-form-field appearance="outline" class="filter">
            <mat-label>{{ f.title }}</mat-label>
            <mat-select [ngModel]="filters()[f.name] ?? ''" (ngModelChange)="setFilter(f.name, $event)">
              <mat-option value="">Все</mat-option>
              @if (f.type === 'bool') {
                <mat-option value="true">Да</mat-option>
                <mat-option value="false">Нет</mat-option>
              } @else {
                @for (v of f.enumValues ?? []; track v) { <mat-option [value]="v">{{ f.enumTitles?.[v] ?? v }}</mat-option> }
              }
            </mat-select>
          </mat-form-field>
        }
      </div>
      @if (loading() && rows().length) { <mat-progress-bar mode="indeterminate" /> }
      @if (state() !== 'ready') {
        <app-view-state [state]="state()" [errorText]="error()" (retry)="load()" />
      } @else {
        <div class="table-wrap">
          <table mat-table [dataSource]="rows()" matSort (matSortChange)="onSort($event)" class="full-width">
            @for (f of columns(); track f.name) {
              <ng-container [matColumnDef]="f.name">
                <th mat-header-cell *matHeaderCellDef mat-sort-header [disabled]="f.type === 'ref'">{{ f.title }}</th>
                <td mat-cell *matCellDef="let row">
                  @if (f.type === 'enum') {
                    <mat-chip-set><mat-chip [class]="'chip-' + row[f.name]">{{ cell(row, f) }}</mat-chip></mat-chip-set>
                  } @else {
                    {{ cell(row, f) }}
                  }
                </td>
              </ng-container>
            }
            <tr mat-header-row *matHeaderRowDef="columnNames()"></tr>
            <tr mat-row *matRowDef="let row; columns: columnNames()" class="clickable" (click)="open(row)" (keydown.enter)="open(row)" tabindex="0"></tr>
          </table>
        </div>
        <mat-paginator [length]="total()" [pageIndex]="page() - 1" [pageSize]="pageSize()" [pageSizeOptions]="[10, 20, 50, 100]" (page)="onPage($event)" />
      }
    </mat-card>
  `,
})
export class EntityListComponent {
  private readonly api = inject(ApiService);
  private readonly auth = inject(AuthService);
  private readonly router = inject(Router);
  readonly entity = input.required<string>();

  protected readonly spec = computed(() => this.api.entity(this.entity()));
  protected readonly columns = computed(() =>
    this.spec().fields.filter((f) => f.listVisible && !isGeo(f) && f.type !== 'json' && f.type !== 'text').slice(0, 7));
  protected readonly columnNames = computed(() => this.columns().map((f) => f.name));
  protected readonly filterFields = computed(() => this.spec().fields.filter((f) => f.type === 'enum' || f.type === 'bool'));
  protected readonly canCreate = computed(() => this.auth.can(this.spec().name, 'create'));

  protected readonly rows = signal<Row[]>([]);
  protected readonly total = signal(0);
  protected readonly page = signal(1);
  protected readonly pageSize = signal(20);
  protected readonly sort = signal<Sort>({ active: '', direction: '' });
  protected readonly q = signal('');
  protected readonly filters = signal<Record<string, string | undefined>>({});
  protected readonly loading = signal(true);
  protected readonly error = signal('');
  protected readonly state = computed<ViewState>(() => {
    if (this.error()) return this.error() === 'forbidden' ? 'forbidden' : 'error';
    if (this.loading() && !this.rows().length) return 'loading';
    return this.rows().length ? 'ready' : 'empty';
  });
  protected readonly search$ = new Subject<string>();

  constructor() {
    this.search$.pipe(debounceTime(300)).subscribe((q) => {
      this.q.set(q);
      this.page.set(1);
      this.load();
    });
    effect(() => {
      this.entity();
      this.page.set(1);
      this.filters.set({});
      this.q.set('');
      this.load();
    });
  }

  load(): void {
    this.loading.set(true);
    this.error.set('');
    const s = this.sort();
    this.api.list(this.spec().name, {
      page: this.page(), pageSize: this.pageSize(), q: this.q(),
      sort: s.direction ? s.active : '', dir: s.direction, filters: this.filters(),
    }).subscribe({
      next: (r) => {
        this.rows.set(r.items);
        this.total.set(r.total);
        this.loading.set(false);
      },
      error: (e) => {
        this.loading.set(false);
        this.error.set(e?.status === 403 ? 'forbidden' : problemMessage(e));
      },
    });
  }

  protected cell(row: Row, f: FieldSpec): string {
    return display(row, f);
  }

  protected setFilter(name: string, value: string): void {
    this.filters.update((f) => ({ ...f, [name]: value }));
    this.page.set(1);
    this.load();
  }

  protected onSort(s: Sort): void {
    this.sort.set(s);
    this.load();
  }

  protected onPage(e: PageEvent): void {
    this.page.set(e.pageIndex + 1);
    this.pageSize.set(e.pageSize);
    this.load();
  }

  protected open(row: Row): void {
    void this.router.navigate(['/e', this.spec().route, row.id]);
  }
}

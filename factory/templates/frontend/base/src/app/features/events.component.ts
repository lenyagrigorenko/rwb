import { JsonPipe } from '@angular/common';
import { Component, OnDestroy, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { MatCardModule } from '@angular/material/card';
import { MatExpansionModule } from '@angular/material/expansion';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatIconModule } from '@angular/material/icon';
import { MatPaginatorModule, PageEvent } from '@angular/material/paginator';
import { MatSelectModule } from '@angular/material/select';
import { MatTooltipModule } from '@angular/material/tooltip';
import { ApiService, DomainEvent } from '../core/api.service';
import { formatDateTime } from '../core/format';
import { APP_SPEC } from '../generated/app-spec';
import { ViewState, ViewStateComponent } from '../shared/view-state.component';

/** Журнал доменных событий: outbox → Kafka → консьюмер (отметка «обработано»). */
@Component({
  selector: 'app-events',
  imports: [JsonPipe, FormsModule, MatCardModule, MatExpansionModule, MatFormFieldModule, MatSelectModule, MatIconModule, MatPaginatorModule, MatTooltipModule, ViewStateComponent],
  template: `
    <div class="page-header"><h1>События</h1></div>
    <mat-card appearance="outlined">
      <div class="toolbar-row">
        <mat-form-field appearance="outline" class="filter">
          <mat-label>Тип события</mat-label>
          <mat-select [(ngModel)]="type" (ngModelChange)="page = 1; load()">
            <mat-option value="">Все</mat-option>
            @for (t of types; track t[0]) { <mat-option [value]="t[0]">{{ t[1] }}</mat-option> }
          </mat-select>
        </mat-form-field>
      </div>
      @if (state() !== 'ready') {
        <app-view-state [state]="state()" emptyText="Событий пока нет" (retry)="load()" />
      } @else {
        <mat-accordion>
          @for (ev of rows(); track ev.id) {
            <mat-expansion-panel>
              <mat-expansion-panel-header>
                <mat-panel-title>
                  <mat-icon [matTooltip]="ev.consumed ? 'Обработано консьюмером Kafka' : 'Ожидает обработки'">{{ ev.consumed ? 'task_alt' : 'schedule' }}</mat-icon>
                  &nbsp;{{ title(ev.type) }}
                </mat-panel-title>
                <mat-panel-description>{{ when(ev.occurredAt) }} · {{ ev.topic }} · v{{ ev.version }}</mat-panel-description>
              </mat-expansion-panel-header>
              <pre class="mono">{{ ev.payload | json }}</pre>
            </mat-expansion-panel>
          }
        </mat-accordion>
        <mat-paginator [length]="total()" [pageIndex]="page - 1" [pageSize]="20" (page)="onPage($event)" />
      }
    </mat-card>
  `,
})
export class EventsComponent implements OnDestroy {
  private readonly api = inject(ApiService);
  protected readonly types = Object.entries(APP_SPEC.eventTitles);
  protected readonly rows = signal<DomainEvent[]>([]);
  protected readonly total = signal(0);
  protected readonly state = signal<ViewState>('loading');
  protected type = '';
  protected page = 1;
  private readonly timer = setInterval(() => this.load(), 5000);

  constructor() {
    this.load();
  }

  ngOnDestroy(): void {
    clearInterval(this.timer);
  }

  load(): void {
    this.api.events({ page: this.page, pageSize: 20, type: this.type }).subscribe({
      next: (r) => {
        this.rows.set(r.items);
        this.total.set(r.total);
        this.state.set(r.items.length ? 'ready' : 'empty');
      },
      error: (e) => this.state.set(e?.status === 403 ? 'forbidden' : 'error'),
    });
  }

  protected onPage(e: PageEvent): void {
    this.page = e.pageIndex + 1;
    this.load();
  }

  protected title(type: string): string {
    return APP_SPEC.eventTitles[type] ?? type;
  }

  protected when(v: string): string {
    return formatDateTime(v);
  }
}

import { Component, OnDestroy, computed, inject, signal } from '@angular/core';
import { MatButtonModule } from '@angular/material/button';
import { MatCardModule } from '@angular/material/card';
import { MatIconModule } from '@angular/material/icon';
import { MatListModule } from '@angular/material/list';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { ApiService, DemoStatus, DomainEvent } from '../core/api.service';
import { formatDateTime } from '../core/format';
import { APP_SPEC } from '../generated/app-spec';
import { GeoMapComponent } from '../shared/geo-map.component';

/** Демонстрационный сценарий из требований: запуск одной кнопкой, ход шагов, карта и события вживую. */
@Component({
  selector: 'app-demo',
  imports: [MatCardModule, MatButtonModule, MatIconModule, MatListModule, MatProgressBarModule, GeoMapComponent],
  template: `
    <div class="page-header">
      <div>
        <h1>Демо-сценарий</h1>
        <p class="muted">{{ status()?.title }}. Шаги взяты из раздела «Демонстрационный сценарий» требований и выполняются через API от имени ролей.</p>
      </div>
      <div class="actions">
        <button mat-stroked-button (click)="advance(10)" [disabled]="status()?.running"><mat-icon>fast_forward</mat-icon>+10 мин</button>
        <button mat-flat-button (click)="run()" [disabled]="status()?.running"><mat-icon>play_arrow</mat-icon>Запустить</button>
      </div>
    </div>
    @if (status()?.running) { <mat-progress-bar mode="indeterminate" /> }
    <div class="demo-grid">
      <mat-card appearance="outlined">
        <mat-card-header>
          <mat-card-title>Шаги · {{ done() }}/{{ status()?.steps?.length ?? 0 }}</mat-card-title>
          <mat-card-subtitle>Сдвиг часов: {{ offset() }} мин</mat-card-subtitle>
        </mat-card-header>
        <mat-card-content>
          <mat-list>
            @for (s of status()?.steps ?? []; track s.id) {
              <mat-list-item [class]="'step step-' + s.status">
                <mat-icon matListItemIcon>{{ icon(s.status) }}</mat-icon>
                <span matListItemTitle>{{ s.title }}</span>
                @if (s.detail) { <span matListItemLine class="step-detail">{{ s.detail }}</span> }
              </mat-list-item>
            }
          </mat-list>
        </mat-card-content>
      </mat-card>
      <div class="demo-side">
        @if (hasGeo) {
          <mat-card appearance="outlined">
            <mat-card-content><app-geo-map height="420px" [refreshMs]="1500" /></mat-card-content>
          </mat-card>
        }
        <mat-card appearance="outlined">
          <mat-card-header><mat-card-title>События (Kafka)</mat-card-title></mat-card-header>
          <mat-card-content>
            <mat-list>
              @for (ev of events(); track ev.id) {
                <mat-list-item>
                  <mat-icon matListItemIcon>{{ ev.consumed ? 'task_alt' : 'schedule' }}</mat-icon>
                  <span matListItemTitle>{{ eventTitle(ev.type) }}</span>
                  <span matListItemLine>{{ when(ev.occurredAt) }} · {{ ev.topic }}</span>
                </mat-list-item>
              }
            </mat-list>
          </mat-card-content>
        </mat-card>
      </div>
    </div>
  `,
})
export class DemoComponent implements OnDestroy {
  private readonly api = inject(ApiService);
  protected readonly status = signal<DemoStatus | null>(null);
  protected readonly events = signal<DomainEvent[]>([]);
  protected readonly done = computed(() => this.status()?.steps.filter((s) => s.status === 'done').length ?? 0);
  protected readonly offset = computed(() => Math.round(this.status()?.clockOffsetMinutes ?? 0));
  protected readonly hasGeo = APP_SPEC.entities.some((e) => e.geo);
  private readonly timer = setInterval(() => this.refresh(), 1000);

  constructor() {
    this.refresh();
  }

  ngOnDestroy(): void {
    clearInterval(this.timer);
  }

  protected run(): void {
    this.api.runDemo().subscribe({ next: (s) => this.status.set(s), error: () => this.refresh() });
  }

  protected advance(minutes: number): void {
    this.api.advanceClock(minutes).subscribe(() => this.refresh());
  }

  private refresh(): void {
    this.api.demo().subscribe({ next: (s) => this.status.set(s), error: () => undefined });
    this.api.events({ pageSize: 10 }).subscribe({ next: (r) => this.events.set(r.items), error: () => undefined });
  }

  protected icon(status: string): string {
    return { pending: 'radio_button_unchecked', running: 'autorenew', done: 'check_circle', failed: 'cancel' }[status] ?? 'help';
  }

  protected eventTitle(type: string): string {
    return APP_SPEC.eventTitles[type] ?? type;
  }

  protected when(v: string): string {
    return formatDateTime(v);
  }
}

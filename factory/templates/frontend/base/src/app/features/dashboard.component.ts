import { Component, OnDestroy, computed, inject, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import { MatButtonModule } from '@angular/material/button';
import { MatCardModule } from '@angular/material/card';
import { MatIconModule } from '@angular/material/icon';
import { MatListModule } from '@angular/material/list';
import { ApiService, Dashboard, DomainEvent } from '../core/api.service';
import { AuthService } from '../core/auth.service';
import { formatDateTime } from '../core/format';
import { WidgetSpec } from '../core/spec.types';
import { APP_SPEC } from '../generated/app-spec';
import { GeoMapComponent } from '../shared/geo-map.component';
import { ViewStateComponent } from '../shared/view-state.component';

/** Обзор: показатели и виджеты экрана из IR (screens, platform=web) для роли пользователя. */
@Component({
  selector: 'app-dashboard',
  imports: [RouterLink, MatCardModule, MatIconModule, MatListModule, MatButtonModule, GeoMapComponent, ViewStateComponent],
  template: `
    <div class="page-header">
      <div>
        <h1>{{ screen()?.title ?? spec.productName }}</h1>
        <p class="muted">{{ spec.summary }}</p>
      </div>
      @if (auth.isAdmin() && spec.hasDemo) {
        <a mat-flat-button routerLink="/demo"><mat-icon>play_circle</mat-icon>Демо-сценарий</a>
      }
    </div>
    @if (!data() && auth.isAdmin()) { <app-view-state state="loading" /> }
    <div class="kpi-grid">
      @for (k of kpis(); track k.title) {
        <mat-card appearance="outlined" class="kpi">
          <div class="kpi-value">{{ k.value }}</div>
          <div class="kpi-title">{{ k.title }}</div>
        </mat-card>
      }
    </div>
    <div class="widget-grid">
      @for (w of widgets(); track $index) {
        @switch (w.type) {
          @case ('map') {
            <mat-card appearance="outlined" class="widget-wide">
              <mat-card-header><mat-card-title>{{ w.title }}</mat-card-title></mat-card-header>
              <mat-card-content><app-geo-map [layers]="layersOf(w)" height="460px" /></mat-card-content>
            </mat-card>
          }
          @case ('event_feed') {
            <mat-card appearance="outlined">
              <mat-card-header><mat-card-title>{{ w.title }}</mat-card-title></mat-card-header>
              <mat-card-content>
                @if (!events().length) { <p class="muted">Событий пока нет</p> }
                <mat-list>
                  @for (ev of events(); track ev.id) {
                    <mat-list-item>
                      <mat-icon matListItemIcon>bolt</mat-icon>
                      <span matListItemTitle>{{ eventTitle(ev.type) }}</span>
                      <span matListItemLine>{{ when(ev.occurredAt) }}</span>
                    </mat-list-item>
                  }
                </mat-list>
              </mat-card-content>
            </mat-card>
          }
        }
      }
    </div>
  `,
})
export class DashboardComponent implements OnDestroy {
  private readonly api = inject(ApiService);
  protected readonly auth = inject(AuthService);
  protected readonly spec = APP_SPEC;
  protected readonly data = signal<Dashboard | null>(null);
  protected readonly events = signal<DomainEvent[]>([]);
  private readonly timer = setInterval(() => this.load(), 5000);

  protected readonly screen = computed(() => {
    const role = this.auth.user()?.role ?? '';
    return APP_SPEC.screens.find((s) => s.roles.includes(role)) ?? null;
  });
  protected readonly widgets = computed<WidgetSpec[]>(() => {
    const own = this.screen()?.widgets.filter((w) => w.type !== 'kpi') ?? [];
    if (own.length) return own;
    const geo = APP_SPEC.entities.some((e) => e.geo && this.auth.canRead(e.name));
    return [
      ...(geo ? [{ type: 'map', title: 'Карта', props: {} } as WidgetSpec] : []),
      ...(this.auth.isAdmin() ? [{ type: 'event_feed', title: 'Последние события', props: {} } as WidgetSpec] : []),
    ];
  });
  protected readonly kpis = computed(() => {
    const d = this.data();
    if (!d) return [];
    const configured = this.screen()?.widgets.filter((w) => w.type === 'kpi') ?? [];
    const items = configured.length
      ? configured.map((w) => ({ title: w.title, value: this.kpiValue(d, w) }))
      : APP_SPEC.entities.map((e) => ({ title: e.titlePlural, value: d.totals[e.name] ?? 0 }));
    return [...items, { title: 'Событий за час', value: d.eventsLastHour }];
  });

  constructor() {
    this.load();
  }

  ngOnDestroy(): void {
    clearInterval(this.timer);
  }

  private load(): void {
    if (!this.auth.isAdmin()) return;
    this.api.dashboard().subscribe({ next: (d) => this.data.set(d), error: () => undefined });
    this.api.events({ pageSize: 8 }).subscribe({ next: (r) => this.events.set(r.items), error: () => undefined });
  }

  private kpiValue(d: Dashboard, w: WidgetSpec): number {
    const filter = (w.props?.['filter'] ?? null) as Record<string, string> | null;
    if (w.entity && filter && Object.keys(filter).length) {
      const value = Object.values(filter)[0];
      return d.statuses[w.entity]?.[value] ?? 0;
    }
    return w.entity ? d.totals[w.entity] ?? 0 : 0;
  }

  protected layersOf(w: WidgetSpec): string[] {
    return (w.props?.['layers'] as string[] | undefined) ?? (w.entity ? [w.entity] : []);
  }

  protected eventTitle(type: string): string {
    return APP_SPEC.eventTitles[type] ?? type;
  }

  protected when(v: string): string {
    return formatDateTime(v);
  }
}

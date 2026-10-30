import { Component, OnDestroy, inject, signal } from '@angular/core';
import { MatCardModule } from '@angular/material/card';
import { MatIconModule } from '@angular/material/icon';
import { MatListModule } from '@angular/material/list';
import { ApiService, AppNotification } from '../core/api.service';
import { formatDateTime } from '../core/format';
import { ViewState, ViewStateComponent } from '../shared/view-state.component';

@Component({
  selector: 'app-notifications',
  imports: [MatCardModule, MatIconModule, MatListModule, ViewStateComponent],
  template: `
    <div class="page-header"><h1>Уведомления</h1></div>
    <mat-card appearance="outlined">
      @if (state() !== 'ready') {
        <app-view-state [state]="state()" emptyText="Уведомлений нет" (retry)="load()" />
      } @else {
        <mat-list>
          @for (n of items(); track n.id) {
            <mat-list-item>
              <mat-icon matListItemIcon>notifications</mat-icon>
              <span matListItemTitle>{{ n.body }}</span>
              <span matListItemLine>{{ n.title }} · {{ when(n.createdAt) }}</span>
            </mat-list-item>
          }
        </mat-list>
      }
    </mat-card>
  `,
})
export class NotificationsComponent implements OnDestroy {
  private readonly api = inject(ApiService);
  protected readonly items = signal<AppNotification[]>([]);
  protected readonly state = signal<ViewState>('loading');
  private readonly timer = setInterval(() => this.load(), 5000);

  constructor() {
    this.load();
  }

  ngOnDestroy(): void {
    clearInterval(this.timer);
  }

  load(): void {
    this.api.notifications().subscribe({
      next: (n) => {
        this.items.set(n);
        this.state.set(n.length ? 'ready' : 'empty');
      },
      error: () => this.state.set('error'),
    });
  }

  protected when(v: string): string {
    return formatDateTime(v);
  }
}

import { Component, input, output } from '@angular/core';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';

export type ViewState = 'loading' | 'empty' | 'error' | 'forbidden' | 'ready';

/** Единое отображение состояний: загрузка, пусто, ошибка, нет доступа. */
@Component({
  selector: 'app-view-state',
  imports: [MatProgressSpinnerModule, MatIconModule, MatButtonModule],
  template: `
    @switch (state()) {
      @case ('loading') {
        <div class="state" role="status" aria-live="polite">
          <mat-spinner diameter="40" />
          <span>Загрузка…</span>
        </div>
      }
      @case ('empty') {
        <div class="state">
          <mat-icon class="state-icon">inbox</mat-icon>
          <span>{{ emptyText() }}</span>
        </div>
      }
      @case ('error') {
        <div class="state state-error" role="alert">
          <mat-icon class="state-icon">error_outline</mat-icon>
          <span>{{ errorText() || 'Не удалось загрузить данные' }}</span>
          <button mat-stroked-button (click)="retry.emit()">Повторить</button>
        </div>
      }
      @case ('forbidden') {
        <div class="state">
          <mat-icon class="state-icon">lock</mat-icon>
          <span>Недостаточно прав для просмотра</span>
        </div>
      }
    }
  `,
})
export class ViewStateComponent {
  readonly state = input<ViewState>('loading');
  readonly emptyText = input('Записей пока нет');
  readonly errorText = input<string>('');
  readonly retry = output<void>();
}

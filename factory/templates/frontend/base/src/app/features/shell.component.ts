import { BreakpointObserver } from '@angular/cdk/layout';
import { Component, computed, inject } from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatListModule } from '@angular/material/list';
import { MatMenuModule } from '@angular/material/menu';
import { MatSidenavModule } from '@angular/material/sidenav';
import { MatToolbarModule } from '@angular/material/toolbar';
import { map } from 'rxjs';
import { AuthService } from '../core/auth.service';
import { APP_SPEC } from '../generated/app-spec';

interface NavItem {
  link: string;
  title: string;
  icon: string;
}

@Component({
  selector: 'app-shell',
  imports: [RouterOutlet, RouterLink, RouterLinkActive, MatSidenavModule, MatToolbarModule, MatListModule, MatIconModule, MatButtonModule, MatMenuModule],
  template: `
    <mat-sidenav-container class="shell">
      <mat-sidenav #drawer [mode]="mobile() ? 'over' : 'side'" [opened]="!mobile()" class="shell-nav">
        <div class="brand">{{ spec.productName }}</div>
        <mat-nav-list>
          @for (item of nav(); track item.link) {
            <a mat-list-item [routerLink]="item.link" routerLinkActive="active-link" (click)="mobile() && drawer.close()">
              <mat-icon matListItemIcon>{{ item.icon }}</mat-icon>
              <span matListItemTitle>{{ item.title }}</span>
            </a>
          }
        </mat-nav-list>
      </mat-sidenav>
      <mat-sidenav-content>
        <mat-toolbar class="shell-toolbar">
          @if (mobile()) {
            <button mat-icon-button (click)="drawer.toggle()" aria-label="Меню"><mat-icon>menu</mat-icon></button>
          }
          <span class="spacer"></span>
          <button mat-button [matMenuTriggerFor]="userMenu">
            <mat-icon>account_circle</mat-icon>
            {{ auth.user()?.displayName }}
          </button>
          <mat-menu #userMenu="matMenu">
            <button mat-menu-item routerLink="/notifications"><mat-icon>notifications</mat-icon>Уведомления</button>
            <button mat-menu-item (click)="auth.logout()"><mat-icon>logout</mat-icon>Выйти</button>
          </mat-menu>
        </mat-toolbar>
        <main class="content"><router-outlet /></main>
      </mat-sidenav-content>
    </mat-sidenav-container>
  `,
  host: { '[class.mobile]': 'mobile()' },
})
export class ShellComponent {
  protected readonly auth = inject(AuthService);
  protected readonly spec = APP_SPEC;
  protected readonly mobile = toSignal(
    inject(BreakpointObserver).observe('(max-width: 900px)').pipe(map((r) => r.matches)),
    { initialValue: false },
  );

  protected readonly nav = computed<NavItem[]>(() => {
    const items: NavItem[] = [{ link: '/dashboard', title: 'Обзор', icon: 'dashboard' }];
    if (APP_SPEC.entities.some((e) => e.geo && this.auth.canRead(e.name))) {
      items.push({ link: '/map', title: 'Карта', icon: 'map' });
    }
    for (const e of APP_SPEC.entities) {
      if (this.auth.canRead(e.name)) items.push({ link: `/e/${e.route}`, title: e.titlePlural, icon: iconFor(e.name) });
    }
    if (this.auth.isAdmin()) {
      items.push({ link: '/events', title: 'События', icon: 'bolt' });
      if (APP_SPEC.hasDemo) items.push({ link: '/demo', title: 'Демо-сценарий', icon: 'play_circle' });
    }
    items.push({ link: '/notifications', title: 'Уведомления', icon: 'notifications' });
    return items;
  });
}

const ICONS: [RegExp, string][] = [
  [/vehicle|car|truck|fleet/i, 'directions_car'],
  [/zone|area|region|polygon/i, 'layers'],
  [/session|booking|reservation/i, 'timer'],
  [/track|point|route|location/i, 'route'],
  [/user|employee|driver|person|staff/i, 'badge'],
  [/order|request|ticket|task|issue/i, 'assignment'],
  [/item|product|equipment|asset|inventory/i, 'inventory_2'],
  [/event|log|history/i, 'history'],
];

function iconFor(name: string): string {
  return ICONS.find(([re]) => re.test(name))?.[1] ?? 'list_alt';
}

import { Routes } from '@angular/router';
import { authGuard } from './core/http';

export const routes: Routes = [
  { path: 'login', loadComponent: () => import('./features/login.component').then((m) => m.LoginComponent) },
  {
    path: '',
    canActivate: [authGuard],
    loadComponent: () => import('./features/shell.component').then((m) => m.ShellComponent),
    children: [
      { path: '', pathMatch: 'full', redirectTo: 'dashboard' },
      { path: 'dashboard', loadComponent: () => import('./features/dashboard.component').then((m) => m.DashboardComponent) },
      { path: 'map', loadComponent: () => import('./features/map.component').then((m) => m.MapComponent) },
      { path: 'events', loadComponent: () => import('./features/events.component').then((m) => m.EventsComponent) },
      { path: 'demo', loadComponent: () => import('./features/demo.component').then((m) => m.DemoComponent) },
      { path: 'notifications', loadComponent: () => import('./features/notifications.component').then((m) => m.NotificationsComponent) },
      { path: 'e/:entity', loadComponent: () => import('./features/entity-list.component').then((m) => m.EntityListComponent) },
      { path: 'e/:entity/:id', loadComponent: () => import('./features/entity-form.component').then((m) => m.EntityFormComponent) },
    ],
  },
  { path: '**', redirectTo: '' },
];

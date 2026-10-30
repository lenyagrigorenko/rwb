import { HttpClient } from '@angular/common/http';
import { Injectable, computed, inject, signal } from '@angular/core';
import { Router } from '@angular/router';
import { firstValueFrom } from 'rxjs';
import { APP_SPEC } from '../generated/app-spec';

export interface CurrentUser {
  id: string;
  login: string;
  displayName: string;
  role: string;
}

interface LoginResponse {
  token: string;
  expiresAt: string;
  user: CurrentUser;
}

const STORAGE_KEY = `${APP_SPEC.slug}.session`;

/** Сессия пользователя: JWT и профиль, хранится в localStorage. */
@Injectable({ providedIn: 'root' })
export class AuthService {
  private readonly http = inject(HttpClient);
  private readonly router = inject(Router);
  private readonly session = signal<LoginResponse | null>(this.restore());

  readonly user = computed(() => this.session()?.user ?? null);
  readonly token = computed(() => this.session()?.token ?? null);
  readonly isAdmin = computed(() => APP_SPEC.adminRoles.includes(this.user()?.role ?? ''));

  async login(login: string, password: string): Promise<void> {
    const response = await firstValueFrom(
      this.http.post<LoginResponse>('/api/v1/auth/login', { login, password }),
    );
    this.session.set(response);
    localStorage.setItem(STORAGE_KEY, JSON.stringify(response));
  }

  logout(): void {
    this.session.set(null);
    localStorage.removeItem(STORAGE_KEY);
    void this.router.navigate(['/login']);
  }

  /** Право роли текущего пользователя на действие над сущностью (read, read_own, create, ...). */
  can(entity: string, action: string): boolean {
    const role = this.user()?.role;
    return !!role && (APP_SPEC.permissions[role]?.[entity] ?? []).includes(action);
  }

  canRead(entity: string): boolean {
    return this.can(entity, 'read') || this.can(entity, 'read_own');
  }

  private restore(): LoginResponse | null {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return null;
      const parsed = JSON.parse(raw) as LoginResponse;
      return new Date(parsed.expiresAt) > new Date() ? parsed : null;
    } catch {
      return null;
    }
  }
}

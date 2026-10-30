import { HttpClient } from '@angular/common/http';
import { Component, inject, signal } from '@angular/core';
import { FormBuilder, ReactiveFormsModule, Validators } from '@angular/forms';
import { Router } from '@angular/router';
import { MatButtonModule } from '@angular/material/button';
import { MatCardModule } from '@angular/material/card';
import { MatChipsModule } from '@angular/material/chips';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { AuthService } from '../core/auth.service';
import { problemMessage } from '../core/http';
import { APP_SPEC } from '../generated/app-spec';

interface DemoUser {
  login: string;
  role: string;
  displayName: string;
}

@Component({
  selector: 'app-login',
  imports: [ReactiveFormsModule, MatCardModule, MatFormFieldModule, MatInputModule, MatButtonModule, MatChipsModule, MatProgressBarModule],
  template: `
    <div class="login-page">
      <mat-card class="login-card" appearance="outlined">
        @if (busy()) { <mat-progress-bar mode="indeterminate" /> }
        <mat-card-header>
          <mat-card-title>{{ spec.productName }}</mat-card-title>
          <mat-card-subtitle>Вход в систему</mat-card-subtitle>
        </mat-card-header>
        <mat-card-content>
          <form [formGroup]="form" (ngSubmit)="submit()" class="form-grid">
            <mat-form-field appearance="outline">
              <mat-label>Логин</mat-label>
              <input matInput formControlName="login" autocomplete="username" required />
              @if (form.controls.login.hasError('required')) { <mat-error>Введите логин</mat-error> }
            </mat-form-field>
            <mat-form-field appearance="outline">
              <mat-label>Пароль</mat-label>
              <input matInput type="password" formControlName="password" autocomplete="current-password" required />
              @if (form.controls.password.hasError('required')) { <mat-error>Введите пароль</mat-error> }
            </mat-form-field>
            @if (error()) { <p class="error-text" role="alert">{{ error() }}</p> }
            <button mat-flat-button type="submit" [disabled]="busy()">Войти</button>
          </form>
          @if (demoUsers().length) {
            <p class="hint">Демо-учётные записи:</p>
            <mat-chip-set>
              @for (u of demoUsers(); track u.login) {
                <mat-chip (click)="form.patchValue({ login: u.login })">{{ u.login }} · {{ roleTitle(u.role) }}</mat-chip>
              }
            </mat-chip-set>
          }
        </mat-card-content>
      </mat-card>
    </div>
  `,
})
export class LoginComponent {
  private readonly auth = inject(AuthService);
  private readonly router = inject(Router);
  protected readonly spec = APP_SPEC;
  protected readonly busy = signal(false);
  protected readonly error = signal('');
  protected readonly demoUsers = signal<DemoUser[]>([]);
  protected readonly form = inject(FormBuilder).nonNullable.group({
    login: ['', Validators.required],
    password: ['', Validators.required],
  });

  constructor() {
    inject(HttpClient).get<DemoUser[]>('/api/v1/auth/demo-users').subscribe({
      next: (u) => this.demoUsers.set(u),
      error: () => this.demoUsers.set([]),
    });
  }

  protected roleTitle(role: string): string {
    return APP_SPEC.roles.find((r) => r.name === role)?.title ?? role;
  }

  protected async submit(): Promise<void> {
    if (this.form.invalid) {
      this.form.markAllAsTouched();
      return;
    }
    this.busy.set(true);
    this.error.set('');
    try {
      const { login, password } = this.form.getRawValue();
      await this.auth.login(login, password);
      await this.router.navigate(['/']);
    } catch (e) {
      this.error.set(problemMessage(e));
    } finally {
      this.busy.set(false);
    }
  }
}

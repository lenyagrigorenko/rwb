import { HttpErrorResponse, HttpInterceptorFn } from '@angular/common/http';
import { inject } from '@angular/core';
import { CanActivateFn, Router } from '@angular/router';
import { MatSnackBar } from '@angular/material/snack-bar';
import { catchError, throwError } from 'rxjs';
import { AuthService } from './auth.service';

/** Добавляет Bearer-токен; при 401 завершает сессию. */
export const authInterceptor: HttpInterceptorFn = (req, next) => {
  const auth = inject(AuthService);
  const token = auth.token();
  const request = token ? req.clone({ setHeaders: { Authorization: `Bearer ${token}` } }) : req;
  return next(request).pipe(
    catchError((error: HttpErrorResponse) => {
      if (error.status === 401 && !req.url.endsWith('/auth/login')) auth.logout();
      return throwError(() => error);
    }),
  );
};

/** Показывает понятное сообщение об ошибке из ProblemDetails. */
export const errorInterceptor: HttpInterceptorFn = (req, next) => {
  const snack = inject(MatSnackBar);
  return next(req).pipe(
    catchError((error: HttpErrorResponse) => {
      if (error.status !== 401 && req.method !== 'GET') {
        snack.open(problemMessage(error), 'OK', { duration: 6000, panelClass: 'snack-error' });
      }
      return throwError(() => error);
    }),
  );
};

export function problemMessage(error: unknown): string {
  if (!(error instanceof HttpErrorResponse)) return 'Неизвестная ошибка';
  if (error.status === 0) return 'Сервер недоступен. Проверьте подключение.';
  if (error.status === 403) return 'Недостаточно прав для выполнения операции';
  const body = error.error as { title?: string; errors?: Record<string, string[]> } | null;
  const details = body?.errors ? Object.values(body.errors).flat().join('; ') : '';
  return [body?.title ?? `Ошибка ${error.status}`, details].filter(Boolean).join(': ');
}

export const authGuard: CanActivateFn = () => {
  const auth = inject(AuthService);
  return auth.user() ? true : inject(Router).createUrlTree(['/login']);
};

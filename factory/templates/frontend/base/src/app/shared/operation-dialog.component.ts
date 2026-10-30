import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { Component, inject, signal } from '@angular/core';
import { FormControl, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { MAT_DIALOG_DATA, MatDialogModule, MatDialogRef } from '@angular/material/dialog';
import { MatButtonModule } from '@angular/material/button';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { MatSelectModule } from '@angular/material/select';
import { MatSlideToggleModule } from '@angular/material/slide-toggle';
import { firstValueFrom } from 'rxjs';
import { ApiService } from '../core/api.service';
import { display } from '../core/format';
import { problemMessage } from '../core/http';
import { OperationSpec } from '../core/spec.types';

interface Option {
  id: string;
  label: string;
}

/** Форма вызова бизнес-операции: поля запроса из IR, справочники для ссылок, ответ или ошибка. */
@Component({
  selector: 'app-operation-dialog',
  imports: [ReactiveFormsModule, MatDialogModule, MatFormFieldModule, MatInputModule, MatSelectModule, MatSlideToggleModule, MatButtonModule, MatProgressBarModule],
  template: `
    <h2 mat-dialog-title>{{ data.op.title }}</h2>
    <mat-dialog-content>
      @if (busy()) { <mat-progress-bar mode="indeterminate" /> }
      <p class="muted">{{ data.op.description }}</p>
      <form [formGroup]="form" class="form-grid">
        @for (f of data.op.inputs; track f.name) {
          @switch (f.type) {
            @case ('bool') { <mat-slide-toggle [formControlName]="f.name">{{ f.title }}</mat-slide-toggle> }
            @case ('enum') {
              <mat-form-field appearance="outline">
                <mat-label>{{ f.title }}</mat-label>
                <mat-select [formControlName]="f.name">
                  @for (v of f.enumValues ?? []; track v) { <mat-option [value]="v">{{ f.enumTitles?.[v] ?? v }}</mat-option> }
                </mat-select>
              </mat-form-field>
            }
            @case ('ref') {
              <mat-form-field appearance="outline">
                <mat-label>{{ f.title }}</mat-label>
                <mat-select [formControlName]="f.name">
                  @for (o of options()[f.name] ?? []; track o.id) { <mat-option [value]="o.id">{{ o.label }}</mat-option> }
                </mat-select>
              </mat-form-field>
            }
            @default {
              <mat-form-field appearance="outline">
                <mat-label>{{ f.title }}</mat-label>
                <input matInput [type]="['int', 'decimal', 'money'].includes(f.type) ? 'number' : 'text'" [formControlName]="f.name" />
              </mat-form-field>
            }
          }
        }
      </form>
      @if (error()) { <p class="error-text" role="alert">{{ error() }}</p> }
      @if (result()) { <pre class="mono">{{ result() }}</pre> }
    </mat-dialog-content>
    <mat-dialog-actions align="end">
      <button mat-button [mat-dialog-close]="!!result()">Закрыть</button>
      <button mat-flat-button (click)="submit()" [disabled]="busy()">Выполнить</button>
    </mat-dialog-actions>
  `,
})
export class OperationDialogComponent {
  protected readonly data = inject<{ op: OperationSpec; id: string | null }>(MAT_DIALOG_DATA);
  private readonly http = inject(HttpClient);
  private readonly api = inject(ApiService);
  private readonly ref = inject(MatDialogRef<OperationDialogComponent>);
  protected readonly busy = signal(false);
  protected readonly error = signal('');
  protected readonly result = signal('');
  protected readonly options = signal<Record<string, Option[] | undefined>>({});
  protected readonly form = new FormGroup<Record<string, FormControl>>(
    Object.fromEntries(this.data.op.inputs.map((f) => [f.name, new FormControl(f.type === 'bool' ? false : null, f.required ? Validators.required : [])])),
  );

  constructor() {
    for (const f of this.data.op.inputs.filter((x) => x.type === 'ref' && x.ref)) {
      const set = (o: Option[]) => this.options.update((all) => ({ ...all, [f.name]: o }));
      if (f.ref === 'User') {
        this.http.get<{ id: string; displayName: string; login: string; role: string }[]>('/api/v1/users')
          .subscribe({ next: (u) => set(u.map((x) => ({ id: x.id, label: `${x.displayName} (${x.login})` }))), error: () => set([]) });
      } else {
        const target = this.api.entity(f.ref!);
        const label = target.fields.find((x) => x.name === target.labelField);
        this.api.list(target.name, { pageSize: 100 })
          .subscribe({ next: (r) => set(r.items.map((row) => ({ id: row.id, label: label ? display(row, label) : row.id }))), error: () => set([]) });
      }
    }
  }

  protected async submit(): Promise<void> {
    if (this.form.invalid) {
      this.form.markAllAsTouched();
      return;
    }
    const body: Record<string, unknown> = {};
    for (const f of this.data.op.inputs) {
      const v = this.form.controls[f.name].value;
      body[f.name] = ['int', 'decimal', 'money'].includes(f.type) && v !== null && v !== '' ? Number(v) : v === '' ? null : v;
    }
    const path = '/api/v1' + this.data.op.path.replace(/\{\w+\}/, this.data.id ?? '');
    this.busy.set(true);
    this.error.set('');
    try {
      const response = this.data.op.method === 'GET'
        ? await firstValueFrom(this.http.get<unknown>(path, { params: body as Record<string, string> }))
        : await firstValueFrom(this.http.request<unknown>(this.data.op.method, path, { body, headers: { 'Idempotency-Key': crypto.randomUUID() } }));
      this.result.set(JSON.stringify(response, null, 2));
    } catch (e) {
      this.error.set(problemMessage(e as HttpErrorResponse));
    } finally {
      this.busy.set(false);
    }
  }

  close(): void {
    this.ref.close(!!this.result());
  }
}

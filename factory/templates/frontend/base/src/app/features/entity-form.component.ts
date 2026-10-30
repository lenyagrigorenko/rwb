import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { Component, computed, effect, inject, input, signal } from '@angular/core';
import { FormControl, FormGroup, ReactiveFormsModule, ValidatorFn, Validators } from '@angular/forms';
import { Router, RouterLink } from '@angular/router';
import { MatAutocompleteModule } from '@angular/material/autocomplete';
import { MatButtonModule } from '@angular/material/button';
import { MatCardModule } from '@angular/material/card';
import { MatChipsModule } from '@angular/material/chips';
import { MatDialog, MatDialogModule } from '@angular/material/dialog';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatIconModule } from '@angular/material/icon';
import { MatInputModule } from '@angular/material/input';
import { MatListModule } from '@angular/material/list';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { MatSelectModule } from '@angular/material/select';
import { MatSlideToggleModule } from '@angular/material/slide-toggle';
import { MatSnackBar } from '@angular/material/snack-bar';
import { firstValueFrom } from 'rxjs';
import { ApiService, DomainEvent } from '../core/api.service';
import { AuthService } from '../core/auth.service';
import { display, formatDateTime, isGeo } from '../core/format';
import { problemMessage } from '../core/http';
import { FieldSpec, OperationSpec, Row, TransitionSpec } from '../core/spec.types';
import { ConfirmDialogComponent } from '../shared/confirm-dialog.component';
import { GeoPreviewComponent } from '../shared/geo-preview.component';
import { OperationDialogComponent } from '../shared/operation-dialog.component';
import { ViewState, ViewStateComponent } from '../shared/view-state.component';
import { APP_SPEC } from '../generated/app-spec';

interface Option {
  id: string;
  label: string;
}

/** Карточка записи: просмотр, создание, изменение, удаление, переходы жизненного цикла, история событий. */
@Component({
  selector: 'app-entity-form',
  imports: [ReactiveFormsModule, RouterLink, MatCardModule, MatFormFieldModule, MatInputModule, MatSelectModule, MatSlideToggleModule, MatAutocompleteModule, MatButtonModule, MatIconModule, MatChipsModule, MatListModule, MatProgressBarModule, MatDialogModule, ViewStateComponent, GeoPreviewComponent],
  template: `
    <div class="page-header">
      <div>
        <a mat-button [routerLink]="['/e', spec().route]"><mat-icon>arrow_back</mat-icon>{{ spec().titlePlural }}</a>
        <h1>{{ isNew() ? 'Новая запись: ' + spec().title : spec().title + ' ' + label() }}</h1>
      </div>
      <div class="actions">
        @for (t of transitions(); track t.name) {
          <button mat-stroked-button (click)="transition(t)" [disabled]="busy()">{{ t.title }}</button>
        }
        @for (op of operations(); track op.name) {
          <button mat-stroked-button (click)="runOperation(op)" [disabled]="busy()"><mat-icon>bolt</mat-icon>{{ op.title }}</button>
        }
        @if (!isNew() && canDelete()) {
          <button mat-stroked-button color="warn" (click)="remove()" [disabled]="busy()"><mat-icon>delete</mat-icon>Удалить</button>
        }
      </div>
    </div>
    @if (state() !== 'ready') {
      <app-view-state [state]="state()" [errorText]="error()" (retry)="load()" />
    } @else {
      <div class="detail-grid">
        <mat-card appearance="outlined">
          @if (busy()) { <mat-progress-bar mode="indeterminate" /> }
          <mat-card-content>
            <form [formGroup]="form" (ngSubmit)="save()" class="form-grid two-col">
              @for (f of formFields(); track f.name) {
                @switch (f.type) {
                  @case ('bool') {
                    <mat-slide-toggle [formControlName]="f.name">{{ f.title }}</mat-slide-toggle>
                  }
                  @case ('enum') {
                    <mat-form-field appearance="outline">
                      <mat-label>{{ f.title }}</mat-label>
                      <mat-select [formControlName]="f.name" [required]="f.required">
                        @if (!f.required) { <mat-option [value]="null">—</mat-option> }
                        @for (v of f.enumValues ?? []; track v) { <mat-option [value]="v">{{ f.enumTitles?.[v] ?? v }}</mat-option> }
                      </mat-select>
                      <mat-error>{{ errorFor(f) }}</mat-error>
                    </mat-form-field>
                  }
                  @case ('ref') {
                    <mat-form-field appearance="outline">
                      <mat-label>{{ f.title }}</mat-label>
                      <input matInput [matAutocomplete]="auto" [value]="refLabel(f)" (input)="searchRef(f, $any($event.target).value)" (focus)="searchRef(f, '')" [readonly]="form.controls[f.name].disabled" />
                      <mat-autocomplete #auto="matAutocomplete" (optionSelected)="pickRef(f, $event.option.value)">
                        @if (!f.required) { <mat-option [value]="null">—</mat-option> }
                        @for (o of refOptions()[f.name] ?? []; track o.id) { <mat-option [value]="o">{{ o.label }}</mat-option> }
                      </mat-autocomplete>
                      <mat-error>{{ errorFor(f) }}</mat-error>
                    </mat-form-field>
                  }
                  @case ('text') {
                    <mat-form-field appearance="outline" class="span-2">
                      <mat-label>{{ f.title }}</mat-label>
                      <textarea matInput rows="3" [formControlName]="f.name" [required]="f.required"></textarea>
                      <mat-error>{{ errorFor(f) }}</mat-error>
                    </mat-form-field>
                  }
                  @case ('json') {
                    <mat-form-field appearance="outline" class="span-2">
                      <mat-label>{{ f.title }} (JSON)</mat-label>
                      <textarea matInput rows="5" class="mono" [formControlName]="f.name"></textarea>
                      <mat-error>{{ errorFor(f) }}</mat-error>
                    </mat-form-field>
                  }
                  @default {
                    <mat-form-field appearance="outline">
                      <mat-label>{{ f.title }}</mat-label>
                      <input matInput [type]="inputType(f)" [formControlName]="f.name" [required]="f.required" [attr.min]="f.min" [attr.max]="f.max" [attr.maxlength]="f.maxLength" />
                      <mat-error>{{ errorFor(f) }}</mat-error>
                    </mat-form-field>
                  }
                }
              }
              @if (canSave()) {
                <div class="span-2 form-actions">
                  <button mat-flat-button type="submit" [disabled]="busy()"><mat-icon>save</mat-icon>Сохранить</button>
                </div>
              }
            </form>
            @for (f of geoFields(); track f.name) {
              @if (row()?.[f.name]) {
                <h3>{{ f.title }}</h3>
                <app-geo-preview [geometry]="$any(row()?.[f.name])" />
              }
            }
          </mat-card-content>
        </mat-card>
        @if (!isNew() && auth.isAdmin()) {
          <mat-card appearance="outlined">
            <mat-card-header><mat-card-title>История событий</mat-card-title></mat-card-header>
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
      </div>
    }
  `,
})
export class EntityFormComponent {
  private readonly api = inject(ApiService);
  private readonly http = inject(HttpClient);
  private readonly router = inject(Router);
  private readonly snack = inject(MatSnackBar);
  private readonly dialog = inject(MatDialog);
  protected readonly auth = inject(AuthService);
  readonly entity = input.required<string>();
  readonly id = input.required<string>();

  protected readonly spec = computed(() => this.api.entity(this.entity()));
  protected readonly isNew = computed(() => this.id() === 'new');
  protected readonly formFields = computed(() =>
    this.spec().fields.filter((f) => !isGeo(f) && f.name !== this.spec().lifecycleField && !f.readonly));
  protected readonly geoFields = computed(() => this.spec().fields.filter(isGeo));
  protected readonly row = signal<Row | null>(null);
  protected readonly events = signal<DomainEvent[]>([]);
  protected readonly refOptions = signal<Record<string, Option[] | undefined>>({});
  protected readonly refLabels = signal<Record<string, string>>({});
  protected readonly busy = signal(false);
  protected readonly loading = signal(false);
  protected readonly error = signal('');
  protected form = new FormGroup<Record<string, FormControl>>({});

  protected readonly canSave = computed(() => {
    const e = this.spec().name;
    if (this.isNew()) return this.auth.can(e, 'create');
    const own = this.spec().ownerField && this.row()?.[this.spec().ownerField!] === this.auth.user()?.id;
    return this.auth.can(e, 'update') || (!!own && this.auth.can(e, 'update_own'));
  });
  protected readonly canDelete = computed(() => this.auth.can(this.spec().name, 'delete'));
  protected readonly transitions = computed<TransitionSpec[]>(() => {
    const row = this.row();
    const lf = this.spec().lifecycleField;
    if (!row || !lf) return [];
    const role = this.auth.user()?.role ?? '';
    return this.spec().transitions.filter((t) => t.from.includes(String(row[lf])) && t.roles.includes(role));
  });
  protected readonly operations = computed<OperationSpec[]>(() => {
    if (this.isNew()) return [];
    const role = this.auth.user()?.role ?? '';
    return APP_SPEC.operations.filter((op) => op.entity === this.spec().name && op.roles.includes(role));
  });
  protected readonly label = computed(() => {
    const row = this.row();
    if (!row) return '';
    const f = this.spec().fields.find((x) => x.name === this.spec().labelField);
    return f ? display(row, f) : '';
  });
  protected readonly state = computed<ViewState>(() => {
    if (this.error()) return this.error() === 'forbidden' ? 'forbidden' : 'error';
    return this.loading() ? 'loading' : 'ready';
  });

  constructor() {
    effect(() => {
      this.entity();
      this.id();
      this.load();
    });
  }

  load(): void {
    this.buildForm();
    this.error.set('');
    if (this.isNew()) {
      this.row.set(null);
      return;
    }
    this.loading.set(true);
    this.api.get(this.spec().name, this.id()).subscribe({
      next: (row) => {
        this.row.set(row);
        this.patch(row);
        this.loading.set(false);
        if (this.auth.isAdmin()) this.api.events({ entityId: row.id, pageSize: 20 }).subscribe((r) => this.events.set(r.items));
      },
      error: (e: HttpErrorResponse) => {
        this.loading.set(false);
        this.error.set(e.status === 403 ? 'forbidden' : problemMessage(e));
      },
    });
  }

  private buildForm(): void {
    const controls: Record<string, FormControl> = {};
    for (const f of this.formFields()) {
      controls[f.name] = new FormControl(f.type === 'bool' ? false : null, this.validators(f));
    }
    this.form = new FormGroup(controls);
    if (!this.isNew() && !this.canSave()) this.form.disable();
  }

  private validators(f: FieldSpec): ValidatorFn[] {
    const v: ValidatorFn[] = [];
    if (f.required && f.type !== 'bool') v.push(Validators.required);
    if (f.maxLength && f.type !== 'text') v.push(Validators.maxLength(f.maxLength));
    if (f.regex) v.push(Validators.pattern(new RegExp(f.regex)));
    if (f.type === 'email') v.push(Validators.email);
    if (f.min !== null && f.min !== undefined) v.push(Validators.min(f.min));
    if (f.max !== null && f.max !== undefined) v.push(Validators.max(f.max));
    if (f.type === 'json') v.push((c) => { try { if (c.value) JSON.parse(c.value); return null; } catch { return { json: true }; } });
    return v;
  }

  private patch(row: Row): void {
    const labels: Record<string, string> = {};
    for (const f of this.formFields()) {
      let value = row[f.name] ?? null;
      if (f.type === 'datetime' && value) value = toLocalInput(String(value));
      if (f.type === 'json' && value) value = JSON.stringify(value, null, 2);
      if (f.type === 'ref' && value) labels[f.name] = row.labels?.[f.name] ?? String(value);
      this.form.controls[f.name]?.setValue(value);
    }
    this.refLabels.set(labels);
    if (!this.canSave()) this.form.disable();
    else this.form.enable();
  }

  protected inputType(f: FieldSpec): string {
    return { int: 'number', decimal: 'number', money: 'number', date: 'date', datetime: 'datetime-local', email: 'email', url: 'url', phone: 'tel' }[f.type as string] ?? 'text';
  }

  protected errorFor(f: FieldSpec): string {
    const c = this.form.controls[f.name];
    if (!c?.errors) return '';
    if (c.errors['required']) return `Поле «${f.title}» обязательно`;
    if (c.errors['maxlength']) return `Не более ${f.maxLength} символов`;
    if (c.errors['pattern']) return 'Неверный формат';
    if (c.errors['email']) return 'Неверный адрес почты';
    if (c.errors['min']) return `Не меньше ${f.min}`;
    if (c.errors['max']) return `Не больше ${f.max}`;
    if (c.errors['json']) return 'Некорректный JSON';
    return String(c.errors['server'] ?? 'Неверное значение');
  }

  protected refLabel(f: FieldSpec): string {
    return this.refLabels()[f.name] ?? '';
  }

  protected searchRef(f: FieldSpec, q: string): void {
    const set = (opts: Option[]) => this.refOptions.update((o) => ({ ...o, [f.name]: opts }));
    if (f.ref === 'User') {
      this.http.get<{ id: string; displayName: string; login: string }[]>('/api/v1/users').subscribe({
        next: (users) => set(users.filter((u) => !q || u.displayName.toLowerCase().includes(q.toLowerCase()))
          .map((u) => ({ id: u.id, label: `${u.displayName} (${u.login})` }))),
        error: () => set([]),
      });
      return;
    }
    const target = this.api.entity(f.ref!);
    const labelField = target.fields.find((x) => x.name === target.labelField);
    this.api.list(target.name, { q, pageSize: 20 }).subscribe({
      next: (r) => set(r.items.map((row) => ({ id: row.id, label: labelField ? display(row, labelField) : row.id }))),
      error: () => set([]),
    });
  }

  protected pickRef(f: FieldSpec, option: Option | null): void {
    this.form.controls[f.name].setValue(option?.id ?? null);
    this.refLabels.update((l) => ({ ...l, [f.name]: option?.label ?? '' }));
  }

  protected async save(): Promise<void> {
    if (this.form.invalid) {
      this.form.markAllAsTouched();
      return;
    }
    const body: Record<string, unknown> = {};
    for (const f of this.formFields()) {
      let v = this.form.controls[f.name].value;
      if (v === '') v = null;
      if (f.type === 'datetime' && v) v = new Date(v).toISOString();
      if (f.type === 'json' && v) v = JSON.parse(v);
      if (['int', 'decimal', 'money'].includes(f.type) && v !== null) v = Number(v);
      body[f.name] = v;
    }
    for (const f of this.geoFields()) body[f.name] = this.row()?.[f.name] ?? null;
    this.busy.set(true);
    try {
      const saved = this.isNew()
        ? await firstValueFrom(this.api.create(this.spec().name, body))
        : await firstValueFrom(this.api.update(this.spec().name, this.id(), body));
      this.snack.open('Сохранено', 'OK', { duration: 2500 });
      if (this.isNew()) await this.router.navigate(['/e', this.spec().route, saved.id]);
      else { this.row.set(saved); this.patch(saved); }
    } catch (e) {
      this.applyServerErrors(e);
    } finally {
      this.busy.set(false);
    }
  }

  protected async transition(t: TransitionSpec): Promise<void> {
    this.busy.set(true);
    try {
      const row = await firstValueFrom(this.api.transition(this.spec().name, this.id(), t.path));
      this.row.set(row);
      this.patch(row);
      this.snack.open(`Выполнено: ${t.title}`, 'OK', { duration: 2500 });
    } catch {
      /* сообщение показал errorInterceptor */
    } finally {
      this.busy.set(false);
    }
  }

  protected async runOperation(op: OperationSpec): Promise<void> {
    const changed = await firstValueFrom(this.dialog.open(OperationDialogComponent, { data: { op, id: this.id() }, width: '560px' }).afterClosed());
    if (changed) this.load();
  }

  protected async remove(): Promise<void> {
    const ok = await firstValueFrom(this.dialog.open(ConfirmDialogComponent, {
      data: { title: 'Удалить запись?', text: `${this.spec().title} ${this.label()} будет удалена без возможности восстановления.` },
    }).afterClosed());
    if (!ok) return;
    this.busy.set(true);
    try {
      await firstValueFrom(this.api.remove(this.spec().name, this.id()));
      this.snack.open('Удалено', 'OK', { duration: 2500 });
      await this.router.navigate(['/e', this.spec().route]);
    } catch {
      /* сообщение показал errorInterceptor */
    } finally {
      this.busy.set(false);
    }
  }

  private applyServerErrors(e: unknown): void {
    const errors = (e as HttpErrorResponse)?.error?.errors as Record<string, string[]> | undefined;
    for (const [k, msgs] of Object.entries(errors ?? {})) {
      const c = this.form.controls[k] ?? this.form.controls[k.charAt(0).toLowerCase() + k.slice(1)];
      c?.setErrors({ server: msgs.join('; ') });
      c?.markAsTouched();
    }
  }

  protected eventTitle(type: string): string {
    return APP_SPEC.eventTitles[type] ?? type;
  }

  protected when(v: string): string {
    return formatDateTime(v);
  }
}

function toLocalInput(iso: string): string {
  const d = new Date(iso);
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

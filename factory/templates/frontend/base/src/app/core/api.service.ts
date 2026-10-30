import { HttpClient, HttpParams } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';
import { APP_SPEC } from '../generated/app-spec';
import { EntitySpec, Paged, Row } from './spec.types';

export interface ListParams {
  page?: number;
  pageSize?: number;
  sort?: string;
  dir?: 'asc' | 'desc' | '';
  q?: string;
  filters?: Record<string, string | null | undefined>;
}

/** Обобщённый клиент REST API сущностей (маршруты и поля — из IR). */
@Injectable({ providedIn: 'root' })
export class ApiService {
  private readonly http = inject(HttpClient);

  entity(name: string): EntitySpec {
    const spec = APP_SPEC.entities.find((e) => e.name === name || e.route === name);
    if (!spec) throw new Error(`Unknown entity ${name}`);
    return spec;
  }

  list(entity: string, p: ListParams = {}): Observable<Paged<Row>> {
    let params = new HttpParams()
      .set('page', p.page ?? 1)
      .set('pageSize', p.pageSize ?? 20);
    if (p.sort) params = params.set('sort', p.sort).set('dir', p.dir || 'asc');
    if (p.q) params = params.set('q', p.q);
    for (const [k, v] of Object.entries(p.filters ?? {})) {
      if (v !== null && v !== undefined && v !== '') params = params.set(k, v);
    }
    return this.http.get<Paged<Row>>(`/api/v1/${this.entity(entity).route}`, { params });
  }

  get(entity: string, id: string): Observable<Row> {
    return this.http.get<Row>(`/api/v1/${this.entity(entity).route}/${id}`);
  }

  create(entity: string, body: Record<string, unknown>): Observable<Row> {
    return this.http.post<Row>(`/api/v1/${this.entity(entity).route}`, body);
  }

  update(entity: string, id: string, body: Record<string, unknown>): Observable<Row> {
    return this.http.put<Row>(`/api/v1/${this.entity(entity).route}/${id}`, body);
  }

  remove(entity: string, id: string): Observable<void> {
    return this.http.delete<void>(`/api/v1/${this.entity(entity).route}/${id}`);
  }

  transition(entity: string, id: string, path: string): Observable<Row> {
    return this.http.post<Row>(`/api/v1/${this.entity(entity).route}/${id}/${path}`, {});
  }

  geojson(entity: string): Observable<GeoJSON.FeatureCollection> {
    return this.http.get<GeoJSON.FeatureCollection>(`/api/v1/${this.entity(entity).route}/geojson`);
  }

  dashboard(): Observable<Dashboard> {
    return this.http.get<Dashboard>('/api/v1/dashboard');
  }

  events(params: { page?: number; pageSize?: number; type?: string; entityId?: string }): Observable<Paged<DomainEvent>> {
    let p = new HttpParams().set('page', params.page ?? 1).set('pageSize', params.pageSize ?? 20);
    if (params.type) p = p.set('type', params.type);
    if (params.entityId) p = p.set('entityId', params.entityId);
    return this.http.get<Paged<DomainEvent>>('/api/v1/events', { params: p });
  }

  notifications(): Observable<AppNotification[]> {
    return this.http.get<AppNotification[]>('/api/v1/me/notifications');
  }

  demo(): Observable<DemoStatus> {
    return this.http.get<DemoStatus>('/api/v1/demo');
  }

  runDemo(): Observable<DemoStatus> {
    return this.http.post<DemoStatus>('/api/v1/demo/run', {});
  }

  advanceClock(minutes: number): Observable<{ now: string; offsetMinutes: number }> {
    return this.http.post<{ now: string; offsetMinutes: number }>('/api/v1/demo/clock', { minutes });
  }
}

export interface Dashboard {
  totals: Record<string, number>;
  statuses: Record<string, Record<string, number>>;
  eventsLastHour: number;
  now: string;
}

export interface DomainEvent {
  id: string;
  type: string;
  version: number;
  topic: string;
  key: string | null;
  entityId: string | null;
  payload: Record<string, unknown>;
  occurredAt: string;
  publishedAt: string | null;
  consumed: boolean;
}

export interface AppNotification {
  id: string;
  title: string;
  body: string;
  eventType: string;
  createdAt: string;
  readAt: string | null;
}

export interface DemoStep {
  id: string;
  title: string;
  action: string;
  status: 'pending' | 'running' | 'done' | 'failed';
  detail: string | null;
  at: string | null;
}

export interface DemoStatus {
  title: string;
  running: boolean;
  clockOffsetMinutes: number;
  steps: DemoStep[];
}

export { APP_SPEC };

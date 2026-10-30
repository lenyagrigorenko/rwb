/** Описание приложения, сгенерированное из IR (см. src/app/generated/app-spec.ts). */
export type FieldType =
  | 'string' | 'text' | 'int' | 'decimal' | 'money' | 'bool' | 'date' | 'datetime' | 'enum' | 'ref'
  | 'email' | 'phone' | 'url' | 'json' | 'geo_point' | 'geo_line' | 'geo_polygon' | 'geometry';

export interface FieldSpec {
  name: string;
  title: string;
  type: FieldType;
  required: boolean;
  maxLength?: number | null;
  min?: number | null;
  max?: number | null;
  regex?: string | null;
  enumValues?: string[] | null;
  enumTitles?: Record<string, string> | null;
  ref?: string | null;
  listVisible: boolean;
  readonly: boolean;
}

export interface TransitionSpec {
  name: string;
  path: string;
  title: string;
  from: string[];
  to: string;
  roles: string[];
}

export interface EntitySpec {
  name: string;
  title: string;
  titlePlural: string;
  route: string;
  description: string;
  labelField: string;
  ownerField?: string | null;
  fields: FieldSpec[];
  lifecycleField?: string | null;
  transitions: TransitionSpec[];
  geo: boolean;
  covers: string[];
}

export interface WidgetSpec {
  type: string;
  title: string;
  entity?: string | null;
  operation?: string | null;
  field?: string | null;
  props: Record<string, unknown>;
}

export interface ScreenSpec {
  id: string;
  name: string;
  title: string;
  roles: string[];
  widgets: WidgetSpec[];
  covers: string[];
}

export interface OperationSpec {
  id: string;
  name: string;
  title: string;
  description: string;
  method: 'GET' | 'POST' | 'PUT' | 'DELETE';
  path: string;
  entity: string | null;
  roles: string[];
  inputs: FieldSpec[];
}

export interface RoleSpec {
  name: string;
  title: string;
  login: string;
}

export interface AppSpec {
  slug: string;
  productName: string;
  summary: string;
  mapCenter: [number, number];
  roles: RoleSpec[];
  adminRoles: string[];
  entities: EntitySpec[];
  permissions: Record<string, Record<string, string[]>>;
  screens: ScreenSpec[];
  eventTitles: Record<string, string>;
  operations: OperationSpec[];
  hasDemo: boolean;
}

export interface Paged<T> {
  items: T[];
  total: number;
  page: number;
  pageSize: number;
}

export type Row = Record<string, unknown> & { id: string; labels?: Record<string, string | null> };

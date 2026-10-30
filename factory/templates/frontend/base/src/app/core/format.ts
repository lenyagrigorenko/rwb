import { FieldSpec, Row } from './spec.types';

const dateTime = new Intl.DateTimeFormat('ru-RU', {
  day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit',
  timeZone: 'Europe/Moscow',
});
const date = new Intl.DateTimeFormat('ru-RU', { timeZone: 'Europe/Moscow' });
const number = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 2 });

/** Отображаемое значение поля строки таблицы/карточки. */
export function display(row: Row, f: FieldSpec): string {
  const v = row[f.name];
  if (v === null || v === undefined || v === '') return '—';
  switch (f.type) {
    case 'ref':
      return row.labels?.[f.name] ?? String(v).slice(0, 8);
    case 'enum':
      return f.enumTitles?.[String(v)] ?? String(v);
    case 'bool':
      return v ? 'Да' : 'Нет';
    case 'datetime':
      return dateTime.format(new Date(String(v)));
    case 'date':
      return date.format(new Date(String(v)));
    case 'int':
    case 'decimal':
    case 'money':
      return number.format(Number(v));
    case 'json':
    case 'geo_point':
    case 'geo_line':
    case 'geo_polygon':
    case 'geometry':
      return typeof v === 'object' ? (v as { type?: string }).type ?? 'JSON' : String(v);
    default:
      return String(v);
  }
}

export function formatDateTime(v: string | null | undefined): string {
  return v ? dateTime.format(new Date(v)) : '—';
}

export function isGeo(f: FieldSpec): boolean {
  return f.type.startsWith('geo') || f.type === 'geometry';
}

// Проверка соответствия UI Kit (Angular Material): в шаблонах запрещены «сырые» элементы управления.
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';

const RULES = [
  [/<button(?![^>]*\bmat-(?:flat-|stroked-|raised-|icon-|fab|mini-fab)?button\b)(?![^>]*\bmat-menu-item\b)[^>]*>/g, '<button> без директивы mat-*-button'],
  [/<input(?![^>]*\bmatInput\b)[^>]*>/g, '<input> без matInput'],
  [/<textarea(?![^>]*\bmatInput\b)[^>]*>/g, '<textarea> без matInput'],
  [/<select\b/g, '<select> вместо mat-select'],
  [/<table(?![^>]*\bmat-table\b)[^>]*>/g, '<table> без mat-table'],
];

const files = [];
(function walk(dir) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) walk(p);
    else if (p.endsWith('.ts') || p.endsWith('.html')) files.push(p);
  }
})('src/app');

let violations = 0;
for (const file of files) {
  const text = readFileSync(file, 'utf8');
  for (const [re, message] of RULES) {
    for (const m of text.matchAll(re)) {
      violations++;
      console.error(`${file}: ${message}: ${m[0].slice(0, 80)}`);
    }
  }
}
console.log(`UI Kit lint: ${files.length} файлов, нарушений: ${violations}`);
process.exit(violations ? 1 : 0);

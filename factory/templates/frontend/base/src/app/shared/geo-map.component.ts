import { AfterViewInit, Component, ElementRef, OnDestroy, inject, input, viewChild } from '@angular/core';
import * as L from 'leaflet';
import { forkJoin } from 'rxjs';
import { ApiService } from '../core/api.service';
import { APP_SPEC } from '../generated/app-spec';

const PALETTE = ['#1565c0', '#2e7d32', '#c62828', '#6a1b9a', '#ef6c00', '#00838f'];

/** Карта Leaflet со слоями геосущностей из API (GeoJSON). Слои с точками обновляются периодически. */
@Component({
  selector: 'app-geo-map',
  template: `<div #host class="geo-map" [style.height]="height()" role="region" aria-label="Карта"></div>`,
})
export class GeoMapComponent implements AfterViewInit, OnDestroy {
  private readonly api = inject(ApiService);
  readonly layers = input<string[]>([]);
  readonly height = input('480px');
  readonly refreshMs = input(3000);
  private readonly host = viewChild.required<ElementRef<HTMLDivElement>>('host');
  private map?: L.Map;
  private overlays = new Map<string, L.GeoJSON>();
  private timer?: ReturnType<typeof setInterval>;
  private fitted = false;

  ngAfterViewInit(): void {
    const [lat, lon] = APP_SPEC.mapCenter;
    this.map = L.map(this.host().nativeElement, { preferCanvas: true }).setView([lat, lon], 14);
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 19,
      attribution: '&copy; OpenStreetMap',
    }).addTo(this.map);
    this.load(true);
    this.timer = setInterval(() => this.load(false), this.refreshMs());
  }

  ngOnDestroy(): void {
    clearInterval(this.timer);
    this.map?.remove();
  }

  private names(): string[] {
    const requested = this.layers().length ? this.layers() : APP_SPEC.entities.filter((e) => e.geo).map((e) => e.name);
    return requested.filter((n) => APP_SPEC.entities.some((e) => e.name === n && e.geo));
  }

  private load(initial: boolean): void {
    const names = this.names().filter((n) => initial || this.isDynamic(n));
    if (!names.length || !this.map) return;
    forkJoin(names.map((n) => this.api.geojson(n))).subscribe({
      next: (collections) => collections.forEach((fc, i) => this.draw(names[i], fc)),
      error: () => undefined,
    });
  }

  private isDynamic(name: string): boolean {
    const e = APP_SPEC.entities.find((x) => x.name === name);
    return !!e?.fields.some((f) => f.type === 'geo_point');
  }

  private draw(name: string, fc: GeoJSON.FeatureCollection): void {
    if (!this.map) return;
    const color = PALETTE[APP_SPEC.entities.findIndex((e) => e.name === name) % PALETTE.length];
    const title = APP_SPEC.entities.find((e) => e.name === name)?.title ?? name;
    this.overlays.get(name)?.remove();
    // Немного точек — «живые» объекты (крупные, с подписью); много — история/трек (мелкие, без подписей).
    const points = fc.features.filter((f) => f.geometry?.type === 'Point').length;
    const few = points <= 20;
    const layer = L.geoJSON(fc, {
      style: () => ({ color, weight: 5, opacity: 0.75 }),
      pointToLayer: (_f, latlng) => few
        ? L.circleMarker(latlng, { radius: 9, color: '#fff', weight: 2, fillColor: color, fillOpacity: 1 })
        : L.circleMarker(latlng, { radius: 3, color, weight: 1, fillColor: color, fillOpacity: 0.6 }),
      onEachFeature: (feature, l) => {
        const p = (feature.properties ?? {}) as Record<string, unknown>;
        const rows = Object.entries(p)
          .filter(([k, v]) => !['id', 'label'].includes(k) && v !== null && v !== '')
          .slice(0, 6)
          .map(([k, v]) => `<div><b>${this.fieldTitle(name, k)}:</b> ${String(v)}</div>`)
          .join('');
        l.bindPopup(`<div class="map-popup"><div class="map-popup-title">${title}: ${String(p['label'] ?? '')}</div>${rows}</div>`);
        const label = String(p['label'] ?? '');
        const meaningful = label && !/^[0-9a-f]{8}-[0-9a-f]{4}-/i.test(label);
        if (feature.geometry.type === 'Point' && few && meaningful) {
          l.bindTooltip(label, { permanent: true, direction: 'top', offset: [0, -8] });
        }
      },
    }).addTo(this.map);
    this.overlays.set(name, layer);
    if (!this.fitted && this.isDynamic(name) && fc.features.length) {
      const bounds = layer.getBounds();
      if (bounds.isValid()) this.map.fitBounds(bounds.pad(0.5), { maxZoom: 15 });
      this.fitted = true;
    }
  }

  private fieldTitle(entity: string, field: string): string {
    return APP_SPEC.entities.find((e) => e.name === entity)?.fields.find((f) => f.name === field)?.title ?? field;
  }
}

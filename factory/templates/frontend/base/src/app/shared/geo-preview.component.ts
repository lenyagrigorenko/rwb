import { AfterViewInit, Component, ElementRef, OnDestroy, input, viewChild } from '@angular/core';
import * as L from 'leaflet';

/** Небольшая карта с одной геометрией (просмотр). */
@Component({
  selector: 'app-geo-preview',
  template: `<div #host class="geo-preview" role="img" aria-label="Геометрия на карте"></div>`,
})
export class GeoPreviewComponent implements AfterViewInit, OnDestroy {
  readonly geometry = input.required<GeoJSON.Geometry>();
  private readonly host = viewChild.required<ElementRef<HTMLDivElement>>('host');
  private map?: L.Map;

  ngAfterViewInit(): void {
    this.map = L.map(this.host().nativeElement, { zoomControl: false, attributionControl: false });
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 19 }).addTo(this.map);
    const layer = L.geoJSON(this.geometry(), {
      style: () => ({ color: '#1565c0', weight: 6 }),
      pointToLayer: (_f, latlng) => L.circleMarker(latlng, { radius: 8, color: '#fff', weight: 2, fillColor: '#1565c0', fillOpacity: 1 }),
    }).addTo(this.map);
    const bounds = layer.getBounds();
    if (bounds.isValid()) this.map.fitBounds(bounds.pad(0.8), { maxZoom: 17 });
  }

  ngOnDestroy(): void {
    this.map?.remove();
  }
}

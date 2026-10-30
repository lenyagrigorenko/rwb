import { Component } from '@angular/core';
import { MatCardModule } from '@angular/material/card';
import { GeoMapComponent } from '../shared/geo-map.component';

@Component({
  selector: 'app-map',
  imports: [MatCardModule, GeoMapComponent],
  template: `
    <div class="page-header"><h1>Карта</h1></div>
    <mat-card appearance="outlined"><mat-card-content><app-geo-map height="calc(100vh - 220px)" /></mat-card-content></mat-card>
  `,
})
export class MapComponent {}

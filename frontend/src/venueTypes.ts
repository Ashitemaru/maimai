export interface VenueLocation {
  latitude: number | null;
  longitude: number | null;
  coordinate_system: 'WGS84';
  updated_at?: string;
}

export interface VenuePlace {
  name: string;
  city: string;
  total: number;
  pending_count: number;
  location: VenueLocation | null;
  jackets: {
    chart_key: string;
    title: string;
    jacket_url: string;
    thumbnail_url?: string | null;
    difficulty: number;
    level: string;
  }[];
}

export function venuePoint(location: VenueLocation | null) {
  return location?.latitude != null && location.longitude != null
    ? { latitude: location.latitude, longitude: location.longitude }
    : null;
}

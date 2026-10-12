export interface VenueLocation {
  place_name: string;
  address: string;
  latitude: number | null;
  longitude: number | null;
  coordinate_system: 'WGS84';
  accuracy: 'exact' | 'building' | 'approximate' | 'unknown';
  closed_status: 'unknown' | 'open' | 'closed' | 'probably_closed';
  source?: string;
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

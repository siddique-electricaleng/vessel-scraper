export const TRUSTED_IMAGE_HOSTS = new Set([
  // MarineTraffic
  "photos.marinetraffic.com",
  "thumb.marinetraffic.com",
  "img.marinetraffic.com",
  "marinetraffic.com",
  // VesselFinder
  "cdn.vesselfinder.com",
  "photos.vesselfinder.com",
  "static.vesselfinder.com",
  "static.vesselfinder.net",
  "vesselfinder.com",
  // VesselTracker
  "photos.vesseltracker.com",
  "media.vesseltracker.com",
  "img.vesseltracker.com",
  "vesseltracker.com",
  // FleetPhoto
  "media.fleetphoto.ru",
  "cdn.fleetphoto.ru",
  "fleetphoto.ru",
  "fleetphoto.de",
  // ShipSpotting
  "images.shipspotting.com",
  "img.shipspotting.com",
  "shipspotting.com",
  // FleetMon
  "photos.fleetmon.com",
  "cdn.fleetmon.com",
  "img.fleetmon.com",
  "fleetmon.com",
  // Others
  "maritimeoptima.com",
  "myshiptracking.com",
]);

export const BASE_HEADERS: Record<string, string> = {
  "User-Agent":
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) " +
    "AppleWebKit/537.36 (KHTML, like Gecko) " +
    "Chrome/124.0.0.0 Safari/537.36",
  Accept:
    "text/html,application/xhtml+xml,application/xml;" +
    "q=0.9,image/avif,image/webp,*/*;q=0.8",
  "Accept-Language": "en-US,en;q=0.9",
  DNT: "1",
  Connection: "keep-alive",
  "Upgrade-Insecure-Requests": "1",
};

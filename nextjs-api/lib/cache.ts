import NodeCache from "node-cache";

interface CachedImage {
  bytes: Buffer;
  contentType: string;
}

// TTL = 1 hour, check for expired keys every 10 minutes
export const imageCache = new NodeCache({
  stdTTL: 3600,
  checkperiod: 600,
  maxKeys: 1000,
  useClones: false,
});

export function getCached(key: string): CachedImage | undefined {
  return imageCache.get<CachedImage>(key);
}

export function setCached(key: string, data: CachedImage): void {
  imageCache.set(key, data);
}

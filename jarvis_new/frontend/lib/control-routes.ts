/**
 * The control API endpoints this dashboard actually calls.
 *
 * An allowlist rather than a pattern: the proxy is the dashboard's own back
 * end, not a general way to reach whatever the control API happens to expose.
 * A panel that needs a new endpoint adds a line here, which is a line in a
 * review rather than a silent widening.
 *
 * It lives beside the route instead of inside it because a Next route file
 * may only export route handlers - the build refuses anything else, which is
 * how this module came to exist.
 */
const ALLOWED: Record<string, readonly string[]> = {
  state: ['GET'],
  settings: ['GET', 'POST'],
  providers: ['GET', 'POST'],
  missions: ['GET', 'POST', 'DELETE'],
  runs: ['GET'],
  memory: ['GET', 'POST', 'DELETE'],
  lessons: ['GET', 'POST', 'DELETE'],
  routines: ['GET', 'POST', 'DELETE'],
  content: ['GET', 'POST'],
  workers: ['GET', 'POST'],
  services: ['GET', 'POST'],
  costs: ['GET'],
  orders: ['GET'],
  commands: ['GET', 'POST', 'DELETE'],
};

/**
 * Is this path one of ours, and is this method allowed on it?
 *
 * Only the first segment decides, because that is what names the resource;
 * the rest is an identifier the control API validates itself. Traversal
 * segments are refused outright rather than normalised - there is no correct
 * request that contains one.
 */
export function routeAllowed(path: string[], method: string): boolean {
  if (path.length === 0 || path.length > 4) return false;
  if (path.some((part) => !part || part === '.' || part === '..' || part.includes('/'))) {
    return false;
  }
  const methods = ALLOWED[path[0]];
  return methods !== undefined && methods.includes(method);
}

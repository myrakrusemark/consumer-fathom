/**
 * Per-process identity nonce.
 *
 * Generated once when the agent starts. The heartbeat plugin emits it in
 * every payload; the local-ui plugin serves it back from /api/identity.
 * The consumer dashboard reads the nonce from the latest heartbeat and
 * compares it to the probe response — a match proves the probed URL
 * really is the agent that wrote the heartbeat, not some other agent
 * that happens to be reachable at the same URL (which is the common case
 * when every agent defaults to advertising http://127.0.0.1:8202 and the
 * dashboard's browser is on one specific host).
 *
 * Rotating on every process start is deliberate: a restarted agent gets
 * a fresh nonce, the next heartbeat carries it, and the dashboard picks
 * it up on the next poll. No persistent state required.
 */
import { randomBytes } from "crypto";

export const IDENTITY_NONCE = randomBytes(16).toString("hex");

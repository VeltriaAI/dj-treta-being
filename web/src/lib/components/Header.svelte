<script lang="ts">
  import type { LiveState } from '../types';

  let { s }: { s: LiveState } = $props();

  const dotClass = $derived(s.connected ? (s.stale ? 'stale' : 'live') : 'down');
  const label = $derived(
    s.connected ? (s.stale ? 'connected · no frames' : 'live') : `reconnecting… (#${s.reconnectAttempt})`,
  );
</script>

<header>
  <div class="brand">
    <span class="name">DJ TRETA</span>
    <span class="sub dim">treta.life · live state</span>
  </div>
  <div class="conn mono">
    {#if s.stale && s.lastFrameTs > 0}
      <span class="badge stale-badge">STALE {Math.round((Date.now() - s.lastFrameTs) / 1000)}s</span>
    {/if}
    <span class="dot {dotClass}"></span>
    <span class="dim">{label}</span>
    {#if s.state?.phase}
      <span class="phase">{s.state.phase}</span>
    {/if}
  </div>
</header>

<style>
  header {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 1rem;
    padding: 0.4rem 0.15rem 0.7rem;
    border-bottom: 1px solid var(--panel-edge);
    flex-wrap: wrap;
  }
  .name {
    font-weight: 800;
    letter-spacing: 0.22em;
    color: var(--ember);
    font-size: 1.05rem;
  }
  .sub { margin-left: 0.6rem; font-size: 0.78rem; }
  .conn { display: flex; align-items: center; gap: 0.5rem; font-size: 0.8rem; }
  .dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }
  .dot.live { background: var(--ok); box-shadow: 0 0 8px var(--ok); }
  .dot.stale { background: var(--warn); box-shadow: 0 0 8px var(--warn); }
  .dot.down { background: var(--bad); box-shadow: 0 0 8px var(--bad); }
  .badge {
    padding: 0.1rem 0.45rem;
    border-radius: 4px;
    font-size: 0.7rem;
    font-weight: 700;
  }
  .stale-badge { background: var(--warn); color: #1a1408; }
  .phase {
    color: var(--saryu);
    text-transform: uppercase;
    letter-spacing: 0.12em;
    font-size: 0.72rem;
  }
</style>

<script lang="ts">
  import type { LiveState, MixxxDeckStatus } from '../types';

  let { s }: { s: LiveState } = $props();

  const deckNums = [1, 2] as const;

  function deck(n: 1 | 2): MixxxDeckStatus | null {
    if (!s.decks) return null;
    return n === 1 ? s.decks.deck1 : s.decks.deck2;
  }

  function vu(n: 1 | 2): number {
    const d = n === 1 ? s.live?.deck1 : s.live?.deck2;
    if (!d) return 0;
    return Math.max(d.vu_left ?? 0, d.vu_right ?? 0);
  }

  function fmtTime(secs: number): string {
    if (!secs || secs < 0) return '0:00';
    const m = Math.floor(secs / 60);
    const sec = Math.floor(secs % 60);
    return `${m}:${String(sec).padStart(2, '0')}`;
  }
</script>

<section class="panel">
  <h2>Decks</h2>
  {#if !s.decks}
    <p class="dim">No deck telemetry yet — waiting for mixxx_status frames.</p>
  {:else}
    <div class="decks">
      {#each deckNums as n (n)}
        {@const d = deck(n)}
        <div class="deck" class:playing={d?.playing}>
          <div class="deck-head mono">
            <span class="deck-tag">DECK {n}</span>
            {#if d?.playing}<span class="on-air">ON AIR</span>{/if}
          </div>
          <div class="title" title={d?.title || ''}>
            {d?.title || (d?.track_loaded ? '(untitled)' : '— empty —')}
          </div>
          <div class="meta mono dim">
            <span>{d?.bpm ? d.bpm.toFixed(1) : '—'} BPM</span>
            <span>-{fmtTime(d?.remaining_seconds ?? 0)}</span>
            <span>{fmtTime(d?.position_seconds ?? 0)} / {fmtTime(d?.duration ?? 0)}</span>
          </div>
          <div class="vu"><div class="vu-fill" style="width: {Math.min(100, vu(n) * 100)}%"></div></div>
        </div>
      {/each}
    </div>
    {#if s.state?.current_track?.title}
      <div class="now mono dim">
        now: <span class="now-title">{s.state.current_track.title}</span>
        · mood {s.state.mood || '—'} · {s.state.tracks_played} played
      </div>
    {/if}
  {/if}
</section>

<style>
  .decks { display: grid; grid-template-columns: 1fr 1fr; gap: 0.7rem; }
  @media (max-width: 560px) { .decks { grid-template-columns: 1fr; } }
  .deck {
    border: 1px solid var(--panel-edge);
    border-radius: 6px;
    padding: 0.6rem 0.7rem;
    min-width: 0;
  }
  .deck.playing { border-color: var(--ember-soft); }
  .deck-head { display: flex; justify-content: space-between; font-size: 0.68rem; }
  .deck-tag { color: var(--ink-dim); letter-spacing: 0.15em; }
  .on-air { color: var(--ember); font-weight: 700; letter-spacing: 0.12em; }
  .title {
    margin: 0.35rem 0;
    font-weight: 600;
    font-size: 0.92rem;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .meta { display: flex; gap: 0.8rem; font-size: 0.72rem; flex-wrap: wrap; }
  .vu {
    margin-top: 0.5rem;
    height: 4px;
    background: var(--panel-edge);
    border-radius: 2px;
    overflow: hidden;
  }
  .vu-fill { height: 100%; background: linear-gradient(90deg, var(--saryu), var(--ember)); }
  .now { margin-top: 0.7rem; font-size: 0.75rem; }
  .now-title { color: var(--ink); }
</style>

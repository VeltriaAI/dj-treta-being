<script lang="ts">
  import type { LiveState } from '../types';

  let { s }: { s: LiveState } = $props();
  let box: HTMLDivElement | undefined = $state();

  // Auto-scroll to newest (bottom) whenever the stream grows.
  $effect(() => {
    void s.thinking.length;
    if (box) box.scrollTop = box.scrollHeight;
  });

  function fmtTs(ts?: number): string {
    if (!ts) return '';
    return new Date(ts * 1000).toLocaleTimeString([], { hour12: false });
  }
</script>

<section class="panel">
  <h2>Thinking</h2>
  <div class="stream mono" bind:this={box}>
    {#if s.thinking.length === 0}
      <p class="dim">Quiet. Her thoughts will stream here.</p>
    {/if}
    {#each s.thinking as t, i (i)}
      <div class="line" class:replay={t.replay}>
        <span class="ts dim">{fmtTs(t.ts)}</span>
        <span class="agent" data-agent={t.agent}>{t.agent}</span>
        {#if t.type === 'call'}
          <span class="call">{t.tool}({t.args})</span>
        {:else}
          <span class="text">{t.text}</span>
        {/if}
      </div>
    {/each}
  </div>
</section>

<style>
  .stream {
    max-height: 320px;
    overflow-y: auto;
    font-size: 0.74rem;
    line-height: 1.5;
  }
  .line { display: flex; gap: 0.5rem; align-items: baseline; padding: 0.08rem 0; }
  .line.replay { opacity: 0.55; }
  .ts { flex: 0 0 auto; font-size: 0.66rem; }
  .agent {
    flex: 0 0 auto;
    color: var(--saryu);
    font-weight: 600;
  }
  .agent[data-agent='dj'] { color: var(--ember); }
  .agent[data-agent='planner'] { color: var(--ok); }
  .call { color: var(--ink-dim); font-style: italic; overflow-wrap: anywhere; }
  .text { overflow-wrap: anywhere; }
</style>

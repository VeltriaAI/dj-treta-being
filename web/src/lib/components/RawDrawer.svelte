<script lang="ts">
  import type { LiveState } from '../types';

  let { s }: { s: LiveState } = $props();
  let open = $state(false);
</script>

<section class="panel">
  <h2>
    <button class="toggle mono" onclick={() => (open = !open)}>
      {open ? '▾' : '▸'} unhandled frames ({s.unhandled.length})
    </button>
  </h2>
  {#if open}
    {#if s.unhandled.length === 0}
      <p class="dim">Every frame type is handled. Nothing raw to show.</p>
    {:else}
      <div class="raw mono">
        {#each s.unhandled as u, i (i)}
          <details>
            <summary>{u.event}</summary>
            <pre>{JSON.stringify(u.data, null, 2)}</pre>
          </details>
        {/each}
      </div>
    {/if}
  {/if}
</section>

<style>
  .toggle {
    background: none;
    border: none;
    color: var(--saryu);
    cursor: pointer;
    font-size: 0.72rem;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    padding: 0;
  }
  .raw { max-height: 260px; overflow: auto; font-size: 0.7rem; }
  summary { cursor: pointer; color: var(--ink-dim); }
  pre {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    background: var(--bg);
    padding: 0.4rem;
    border-radius: 4px;
  }
</style>

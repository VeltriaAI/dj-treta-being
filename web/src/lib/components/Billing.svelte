<script lang="ts">
  import type { LiveState } from '../types';

  let { s }: { s: LiveState } = $props();

  const tokens = $derived(
    s.billing ? (s.billing.total_input_tokens ?? 0) + (s.billing.total_output_tokens ?? 0) : 0,
  );

  function fmtTokens(n: number): string {
    if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
    if (n >= 1_000) return `${Math.floor(n / 1_000)}K`;
    return String(n);
  }
</script>

<section class="panel">
  <h2>Billing</h2>
  {#if !s.billing}
    <p class="dim">No billing snapshot yet.</p>
  {:else}
    <div class="cost mono">${(s.billing.total_cost_usd ?? 0).toFixed(4)}</div>
    <div class="row mono dim">
      <span>{fmtTokens(tokens)} tokens</span>
      <span>{s.billing.calls ?? 0} calls</span>
    </div>
  {/if}
</section>

<style>
  .cost { font-size: 1.5rem; font-weight: 700; color: var(--saryu); }
  .row { display: flex; justify-content: space-between; font-size: 0.75rem; margin-top: 0.3rem; }
</style>

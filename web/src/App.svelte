<script lang="ts">
  import { live } from './lib/wsClient';
  import Header from './lib/components/Header.svelte';
  import Decks from './lib/components/Decks.svelte';
  import Thinking from './lib/components/Thinking.svelte';
  import Billing from './lib/components/Billing.svelte';
  import RawDrawer from './lib/components/RawDrawer.svelte';
</script>

<Header s={$live} />

<main>
  <div class="col-main">
    <Decks s={$live} />
    <Thinking s={$live} />
  </div>
  <div class="col-side">
    <Billing s={$live} />
    <RawDrawer s={$live} />
  </div>
</main>

<style>
  main {
    display: grid;
    grid-template-columns: 1fr 280px;
    gap: 0.9rem;
    margin-top: 0.9rem;
  }
  .col-main, .col-side {
    display: flex;
    flex-direction: column;
    gap: 0.9rem;
    min-width: 0;
  }
  @media (max-width: 820px) {
    main { grid-template-columns: 1fr; }
  }
</style>

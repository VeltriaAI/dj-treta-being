import { mount } from 'svelte';
import './app.css';
import App from './App.svelte';
import { client } from './lib/wsClient';

client.start();

const app = mount(App, {
  target: document.getElementById('app') as HTMLElement,
});

export default app;

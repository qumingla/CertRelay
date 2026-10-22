import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import '@fontsource-variable/geist/index.css'
import './index.css'
import App from './App.tsx'

async function init() {
  if (import.meta.env.VITE_USE_MOCKS === 'true') {
    await import('./lib/mock');
  }

  createRoot(document.getElementById('root')!).render(
    <StrictMode>
      <App />
    </StrictMode>,
  )
}

init();

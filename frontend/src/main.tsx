import React from 'react';
import ReactDOM from 'react-dom/client';
import AuthGate from './AuthGate';
import { ToastProvider } from './ToastProvider';
import '../styles.css';
ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <ToastProvider>
      <AuthGate />
    </ToastProvider>
  </React.StrictMode>,
);

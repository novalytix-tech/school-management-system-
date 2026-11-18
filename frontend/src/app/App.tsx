// ==========================================
// School ERP Root Application
// Developed by NTS Novalytix
// ==========================================

import React from 'react';
import { RouterProvider } from 'react-router-dom';
import { router } from './router';
import { ERPProvider } from '../context/ERPContext';
import { TopLoadingBar } from '../components/common/TopLoadingBar';

export const App: React.FC = () => {
  return (
    <ERPProvider>
      <TopLoadingBar />
      <RouterProvider router={router} />
    </ERPProvider>
  );
};

export default App;


import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    globals: false,
    setupFiles: ['./src/test/setup.ts'],
    // Test files live beside the code they cover, so a component and its test
    // move together and a missing test is visible in the same directory
    // listing rather than in a parallel tree nobody opens.
    include: ['src/**/*.{test,spec}.{ts,tsx}'],
  },
})

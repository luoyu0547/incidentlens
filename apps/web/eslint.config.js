import globals from 'globals';
import tseslint from 'typescript-eslint';
import reactHooks from 'eslint-plugin-react-hooks';
import reactRefresh from 'eslint-plugin-react-refresh';

export default tseslint.config(
  {
    ignores: ['dist/**', 'node_modules/**'],
  },
  ...tseslint.configs.recommended,
  {
    plugins: {
      'react-hooks': reactHooks,
      'react-refresh': reactRefresh,
    },
    languageOptions: {
      globals: globals.browser,
      parserOptions: {
        projectService: {
          allowDefaultProject: ['vite.config.ts', 'vitest.config.ts', 'playwright.config.ts'],
        },
        tsconfigRootDir: import.meta.dirname,
      },
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      'react-refresh/only-export-components': ['warn', { allowConstantExport: true }],
      '@typescript-eslint/no-floating-promises': 'error',
      '@typescript-eslint/no-misused-promises': 'error',
      // Read-only boundary: all HTTP flows through the guarded WebReadonlyClient
      // facade. Raw generated SDK clients, generated SDK internals, and
      // openapi-fetch are forbidden in the web workspace.
      'no-restricted-imports': [
        'error',
        {
          paths: [
            {
              name: 'openapi-fetch',
              message:
                'Web code must use the guarded WebReadonlyClient facade from @incidentlens/protocol.',
            },
            {
              name: '@hey-api/client-fetch',
              message:
                'Web code must use the guarded WebReadonlyClient facade from @incidentlens/protocol.',
            },
            {
              name: '@incidentlens/protocol',
              importNames: ['createClient', 'createSdkClient'],
              message:
                'Web code must use the guarded WebReadonlyClient facade from @incidentlens/protocol.',
            },
          ],
          patterns: [
            {
              group: ['**/packages/protocol/src/**', '**/generated/**'],
              message:
                'Web code must not reach into @incidentlens/protocol internals; import the guarded WebReadonlyClient facade from @incidentlens/protocol.',
            },
          ],
        },
      ],
    },
  },
);

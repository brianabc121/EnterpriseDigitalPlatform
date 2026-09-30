// 前端代码检查（设计文档 §20：eslint）：TypeScript 与 Vue 的推荐规则，只检查可能出错的写法，
// 排版交给编辑器。生成的 API 类型、构建产物不检查。
import js from '@eslint/js'
import pluginVue from 'eslint-plugin-vue'
import globals from 'globals'
import tseslint from 'typescript-eslint'

export default tseslint.config(
  {
    ignores: ['**/dist/**', '**/node_modules/**', 'packages/api-client/src/schema.d.ts'],
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  ...pluginVue.configs['flat/essential'],
  {
    files: ['**/*.vue'],
    languageOptions: { parserOptions: { parser: tseslint.parser } },
  },
  {
    languageOptions: {
      ecmaVersion: 'latest',
      sourceType: 'module',
      globals: { ...globals.browser },
    },
    rules: {
      '@typescript-eslint/no-unused-vars': [
        'error',
        { argsIgnorePattern: '^_', varsIgnorePattern: '^_', caughtErrors: 'none' },
      ],
      '@typescript-eslint/consistent-type-imports': ['error', { fixStyle: 'inline-type-imports' }],
      'vue/multi-word-component-names': 'off',
      'vue/no-v-html': 'error',
      eqeqeq: ['error', 'always'],
      'no-console': ['error', { allow: ['warn', 'error'] }],
    },
  },
  {
    // 前端测试与 Vite 配置运行在 Node 里。
    files: ['**/*.test.ts', '**/vite.config.ts'],
    languageOptions: { globals: { ...globals.node } },
  },
)

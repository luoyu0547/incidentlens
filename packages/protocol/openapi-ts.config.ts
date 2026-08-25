/** @type {import('@hey-api/openapi-ts').UserConfig} */
export default {
  input: './openapi/v1.json',
  output: './src/generated',
  plugins: ['@hey-api/typescript', '@hey-api/sdk', '@hey-api/schemas', '@hey-api/client-fetch'],
};

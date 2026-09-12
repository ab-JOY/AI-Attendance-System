/**
 * Axios HTTP client configured for the Flask API.
 *
 * - Reads the JWT token from SecureStore and attaches it as a Bearer header.
 * - Reads the server URL from SecureStore (set on the Settings/Login screen).
 * - Automatically redirects to login on 401.
 */

import axios from 'axios';
import * as SecureStore from 'expo-secure-store';

// Defaults — overridden at runtime from SecureStore
let BASE_URL = 'http://192.168.1.100:5000';

const client = axios.create({
  timeout: 15000,
  headers: {
    'Content-Type': 'application/json',
  },
});

/**
 * Initialize the API client with the stored server URL.
 * Call this once at app start.
 */
export async function initClient() {
  try {
    const storedUrl = await SecureStore.getItemAsync('server_url');
    if (storedUrl) {
      BASE_URL = storedUrl;
    }
  } catch {
    // SecureStore not available (web preview) — use default
  }
}

/**
 * Update the server URL (called from Settings / first-launch screen).
 */
export async function setServerUrl(url) {
  BASE_URL = url.replace(/\/+$/, ''); // strip trailing slashes
  await SecureStore.setItemAsync('server_url', BASE_URL);
}

/**
 * Get the current server URL.
 */
export function getServerUrl() {
  return BASE_URL;
}

// Request interceptor: attach JWT + base URL
client.interceptors.request.use(async (config) => {
  config.baseURL = BASE_URL;

  try {
    const token = await SecureStore.getItemAsync('auth_token');
    if (token) {
      config.headers.Authorization = `Bearer ${token}`;
    }
  } catch {
    // SecureStore not available
  }

  return config;
});

// Response interceptor: handle auth errors
client.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401) {
      // Token expired or invalid — handled by AuthContext
    }
    return Promise.reject(error);
  }
);

export default client;

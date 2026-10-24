/**
 * Authentication context — manages JWT token, user state, login/logout.
 */

import React, { createContext, useContext, useEffect, useState } from 'react';
import * as SecureStore from 'expo-secure-store';
import client, { initClient } from '../api/client';

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null);
  const [token, setToken] = useState(null);
  const [isLoading, setIsLoading] = useState(true);

  // Restore token on app start
  useEffect(() => {
    (async () => {
      try {
        await initClient();
        const storedToken = await SecureStore.getItemAsync('auth_token');
        const storedUser = await SecureStore.getItemAsync('auth_user');

        const restored = storedUser ? JSON.parse(storedUser) : null;

        // An administrator or instructor signed in on this phone before the
        // app became student-only. The server now refuses that token on every
        // route, so start from the login screen instead.
        if (storedToken && restored?.role === 'student') {
          setToken(storedToken);
          setUser(restored);
        } else if (storedToken) {
          await SecureStore.deleteItemAsync('auth_token');
          await SecureStore.deleteItemAsync('auth_user');
        }
      } catch {
        // Token expired or corrupt — stay logged out
      } finally {
        setIsLoading(false);
      }
    })();
  }, []);

  // Students only: administrators and instructors use the website.
  const login = async (userId, password) => {
    let response;

    try {
      response = await client.post('/api/login', {
        role: 'student',
        user_id: userId,
        password,
      });
    } catch (err) {
      // A wrong password is a 401, which axios throws. Uncaught, the login
      // button span for ever and the message never appeared.
      return {
        success: false,
        message: err.response?.data?.message || 'Could not reach the server. Check the server URL in Settings.',
      };
    }

    if (response.data.success) {
      const { token: newToken, role: userRole, display_name } = response.data;
      const userData = { role: userRole, user_id: userId, display_name };

      await SecureStore.setItemAsync('auth_token', newToken);
      await SecureStore.setItemAsync('auth_user', JSON.stringify(userData));

      setToken(newToken);
      setUser(userData);

      return { success: true };
    }

    return { success: false, message: response.data.message };
  };

  const logout = async () => {
    await SecureStore.deleteItemAsync('auth_token');
    await SecureStore.deleteItemAsync('auth_user');
    setToken(null);
    setUser(null);
  };

  return (
    <AuthContext.Provider
      value={{
        user,
        token,
        isLoading,
        isAuthenticated: !!token,
        login,
        logout,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error('useAuth must be used within an AuthProvider');
  }
  return context;
}

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
  // Pending registration token — stored but NOT activated until face capture completes
  const [pendingToken, setPendingToken] = useState(null);
  const [pendingUser, setPendingUser] = useState(null);

  // Restore token on app start
  useEffect(() => {
    (async () => {
      try {
        await initClient();
        const storedToken = await SecureStore.getItemAsync('auth_token');
        const storedUser = await SecureStore.getItemAsync('auth_user');

        if (storedToken && storedUser) {
          setToken(storedToken);
          setUser(JSON.parse(storedUser));
        }
      } catch {
        // Token expired or corrupt — stay logged out
      } finally {
        setIsLoading(false);
      }
    })();
  }, []);

  const login = async (role, userId, password) => {
    const response = await client.post('/api/login', {
      role,
      user_id: userId,
      password,
    });

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

  const register = async (data) => {
    const response = await client.post('/api/students/register', data);

    if (response.data.success) {
      const { token: newToken, role, display_name } = response.data;
      const userData = { role, user_id: data.student_id, display_name };

      // Store token on disk but do NOT activate it yet.
      // The Auth Stack stays active so FaceCapture can run.
      await SecureStore.setItemAsync('auth_token', newToken);
      await SecureStore.setItemAsync('auth_user', JSON.stringify(userData));
      setPendingToken(newToken);
      setPendingUser(userData);

      return { success: true, token: newToken };
    }

    return { success: false, message: response.data.message };
  };

  const completeRegistration = () => {
    // Called after face capture — activates the stored token and flips isAuthenticated.
    if (pendingToken && pendingUser) {
      setToken(pendingToken);
      setUser(pendingUser);
      setPendingToken(null);
      setPendingUser(null);
    }
  };

  const getPendingToken = () => pendingToken;

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
        register,
        completeRegistration,
        getPendingToken,
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

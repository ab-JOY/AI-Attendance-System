/**
 * Settings Screen — Allows user to configure the Server URL on first launch.
 */

import React, { useState, useEffect } from 'react';
import { View, StyleSheet, Text, KeyboardAvoidingView, Platform, ScrollView } from 'react-native';
import { getServerUrl, setServerUrl } from '../api/client';
import Input from '../components/Input';
import Button from '../components/Button';
import Header from '../components/Header';
import { colors, spacing, typography } from '../theme/colors';

export default function SettingsScreen({ navigation }) {
  const [url, setUrl] = useState('');
  const [isSaved, setIsSaved] = useState(false);

  useEffect(() => {
    setUrl(getServerUrl());
  }, []);

  const handleSave = async () => {
    if (!url.trim()) return;
    await setServerUrl(url.trim());
    setIsSaved(true);
    setTimeout(() => {
      navigation.replace('Login');
    }, 1500);
  };

  return (
    <KeyboardAvoidingView
      style={styles.container}
      behavior={Platform.OS === 'ios' ? 'padding' : 'height'}
    >
      <Header title="Server Configuration" />
      <ScrollView contentContainerStyle={styles.content}>
        <Text style={styles.description}>
          Please enter the URL of the AI Attendance System backend server.
          This is typically your computer's IP address on the local network (e.g., http://192.168.1.5:5000).
        </Text>

        <Input
          placeholder="e.g. http://192.168.1.100:5000"
          value={url}
          onChangeText={(text) => {
            setUrl(text);
            setIsSaved(false);
          }}
          icon="server-outline"
          autoCapitalize="none"
          keyboardType="url"
        />

        {isSaved && <Text style={styles.successText}>Server URL saved successfully!</Text>}

        <Button
          title="Save & Continue"
          onPress={handleSave}
          style={styles.saveButton}
          disabled={!url.trim()}
        />
      </ScrollView>
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    backgroundColor: colors.background,
  },
  content: {
    flexGrow: 1,
    padding: spacing.lg,
    justifyContent: 'center',
  },
  description: {
    ...typography.body,
    color: colors.textSecondary,
    marginBottom: spacing.xl,
    textAlign: 'center',
  },
  saveButton: {
    marginTop: spacing.xl,
  },
  successText: {
    ...typography.bodySmall,
    color: colors.success,
    textAlign: 'center',
    marginTop: spacing.md,
  },
});

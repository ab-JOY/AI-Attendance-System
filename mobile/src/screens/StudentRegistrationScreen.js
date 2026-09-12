/**
 * Student Registration Screen.
 * Captures personal and academic info, then navigates to FaceCapture.
 */

import React, { useState } from 'react';
import {
  View,
  StyleSheet,
  Text,
  KeyboardAvoidingView,
  Platform,
  ScrollView,
} from 'react-native';
import { useAuth } from '../context/AuthContext';
import Input from '../components/Input';
import Button from '../components/Button';
import Header from '../components/Header';
import { colors, spacing, typography } from '../theme/colors';

export default function StudentRegistrationScreen({ navigation }) {
  const { register } = useAuth();
  const [formData, setFormData] = useState({
    student_id: '',
    first_name: '',
    middle_name: '',
    last_name: '',
    email: '',
    password: '',
    college_department: '',
    program: '',
    year_level: '',
    section: '',
  });
  const [error, setError] = useState('');
  const [isLoading, setIsLoading] = useState(false);

  const updateForm = (key, value) => {
    setFormData((prev) => ({ ...prev, [key]: value }));
  };

  const handleRegister = async () => {
    if (
      !formData.student_id ||
      !formData.first_name ||
      !formData.last_name ||
      !formData.password
    ) {
      setError('Student number, first name, last name, and password are required.');
      return;
    }

    if (formData.password.length < 8) {
      setError('Password must be at least 8 characters.');
      return;
    }

    setIsLoading(true);
    setError('');

    const result = await register(formData);

    setIsLoading(false);

    if (result.success) {
      // Registration successful, token saved. Now proceed to face capture.
      navigation.replace('FaceCapture', { studentId: formData.student_id, studentName: formData.first_name + ' ' + formData.last_name });
    } else {
      setError(result.message || 'Registration failed');
    }
  };

  return (
    <KeyboardAvoidingView
      style={styles.container}
      behavior={Platform.OS === 'ios' ? 'padding' : 'height'}
    >
      <Header title="Student Registration" onBack={() => navigation.goBack()} />
      <ScrollView contentContainerStyle={styles.content}>
        
        <Text style={styles.sectionTitle}>Personal Information</Text>
        <Input
          placeholder="Student number"
          value={formData.student_id}
          onChangeText={(val) => updateForm('student_id', val)}
          autoCapitalize="none"
        />
        <Input
          placeholder="First Name"
          value={formData.first_name}
          onChangeText={(val) => updateForm('first_name', val)}
        />
        <Input
          placeholder="Middle Name (Optional)"
          value={formData.middle_name}
          onChangeText={(val) => updateForm('middle_name', val)}
        />
        <Input
          placeholder="Last Name"
          value={formData.last_name}
          onChangeText={(val) => updateForm('last_name', val)}
        />
        <Input
          placeholder="Email Address (Optional)"
          value={formData.email}
          onChangeText={(val) => updateForm('email', val)}
          keyboardType="email-address"
          autoCapitalize="none"
        />
        <Input
          placeholder="Password"
          value={formData.password}
          onChangeText={(val) => updateForm('password', val)}
          secureTextEntry
        />

        <Text style={styles.sectionTitle}>Academic Information</Text>
        <Input
          placeholder="College"
          value={formData.college_department}
          onChangeText={(val) => updateForm('college_department', val)}
        />
        <Input
          placeholder="Program"
          value={formData.program}
          onChangeText={(val) => updateForm('program', val)}
        />
        <Input
          placeholder="Year Level"
          value={formData.year_level}
          onChangeText={(val) => updateForm('year_level', val)}
          keyboardType="numeric"
        />
        <Input
          placeholder="Section"
          value={formData.section}
          onChangeText={(val) => updateForm('section', val)}
        />

        {error ? <Text style={styles.errorText}>{error}</Text> : null}

        <Button
          title="Register student & Capture face"
          onPress={handleRegister}
          loading={isLoading}
          style={styles.registerButton}
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
  },
  sectionTitle: {
    ...typography.h3,
    marginTop: spacing.md,
    marginBottom: spacing.sm,
  },
  registerButton: {
    marginTop: spacing.xl,
    marginBottom: spacing.xxl,
  },
  errorText: {
    ...typography.caption,
    color: colors.error,
    textAlign: 'center',
    marginTop: spacing.md,
  },
});

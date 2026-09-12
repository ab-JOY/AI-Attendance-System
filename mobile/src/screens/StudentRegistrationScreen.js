import React, { useState, useEffect } from 'react';
import {
  View,
  StyleSheet,
  Text,
  KeyboardAvoidingView,
  Platform,
  ScrollView,
  Modal,
  TouchableOpacity,
} from 'react-native';
import { useAuth } from '../context/AuthContext';
import client from '../api/client';
import Input from '../components/Input';
import Button from '../components/Button';
import Header from '../components/Header';
import { colors, spacing, typography, borderRadius } from '../theme/colors';

const Dropdown = ({ value, options, onSelect, placeholder }) => {
  const [visible, setVisible] = useState(false);
  return (
    <View style={styles.dropdownContainer}>
      <TouchableOpacity onPress={() => setVisible(true)} style={styles.dropdownButton}>
        <Text style={value ? styles.dropdownText : styles.dropdownPlaceholder}>{value || placeholder}</Text>
      </TouchableOpacity>
      <Modal visible={visible} transparent animationType="fade">
        <TouchableOpacity style={styles.modalOverlay} activeOpacity={1} onPress={() => setVisible(false)}>
          <View style={styles.modalContent}>
             <Text style={styles.modalTitle}>{placeholder}</Text>
             <ScrollView>
               {options.map(opt => (
                  <TouchableOpacity key={opt} onPress={() => { onSelect(opt); setVisible(false); }} style={styles.modalOption}>
                     <Text style={styles.modalOptionText}>{opt}</Text>
                  </TouchableOpacity>
               ))}
               {options.length === 0 && (
                 <Text style={{textAlign: 'center', margin: 10, color: colors.textSecondary}}>No options available</Text>
               )}
             </ScrollView>
          </View>
        </TouchableOpacity>
      </Modal>
    </View>
  );
};

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
      !formData.password ||
      !formData.college_department ||
      !formData.program
    ) {
      setError('Student number, first name, last name, password, college, and program are required.');
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
      navigation.replace('FaceCapture', { studentId: formData.student_id, studentName: formData.first_name + ' ' + formData.last_name });
    } else {
      setError(result.message || 'Registration failed');
    }
  };

  const [colleges, setColleges] = useState([
    'College of Computing',
    'College of Engineering',
    'College of Business',
    'College of Education',
  ]);
  const [programs, setPrograms] = useState([
    'BSCS',
    'BSIT',
    'BSIS',
    'BSEd',
  ]);

  useEffect(() => {
    // Fetch dynamic colleges and programs from DB
    client.get('/api/colleges_programs')
      .then(res => {
        if (res.data.success) {
          if (res.data.colleges && res.data.colleges.length > 0) {
            setColleges(res.data.colleges);
          }
          if (res.data.programs && res.data.programs.length > 0) {
            setPrograms(res.data.programs);
          }
        }
      })
      .catch(err => console.warn('Could not fetch options:', err));
  }, []);

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
        <Dropdown
          placeholder="Select College"
          value={formData.college_department}
          options={colleges}
          onSelect={(val) => updateForm('college_department', val)}
        />
        <Dropdown
          placeholder="Select Program"
          value={formData.program}
          options={programs}
          onSelect={(val) => updateForm('program', val)}
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
  dropdownContainer: {
    marginBottom: spacing.md,
  },
  dropdownButton: {
    backgroundColor: colors.surface,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: borderRadius.md,
    padding: spacing.md,
  },
  dropdownText: {
    ...typography.body,
    color: colors.text,
  },
  dropdownPlaceholder: {
    ...typography.body,
    color: colors.textSecondary,
  },
  modalOverlay: {
    flex: 1,
    backgroundColor: 'rgba(0, 0, 0, 0.5)',
    justifyContent: 'center',
    padding: spacing.xl,
  },
  modalContent: {
    backgroundColor: colors.surface,
    borderRadius: borderRadius.lg,
    maxHeight: '80%',
    padding: spacing.lg,
  },
  modalTitle: {
    ...typography.h3,
    marginBottom: spacing.md,
    textAlign: 'center',
  },
  modalOption: {
    paddingVertical: spacing.md,
    borderBottomWidth: 1,
    borderBottomColor: colors.border,
  },
  modalOptionText: {
    ...typography.body,
    textAlign: 'center',
  },
});

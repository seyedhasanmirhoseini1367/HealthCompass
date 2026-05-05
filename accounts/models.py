from django.contrib.auth.models import AbstractUser
from django.db import models


class CustomUser(AbstractUser):
    class Role(models.TextChoices):
        PATIENT        = 'patient',        'Patient'
        DOCTOR         = 'doctor',         'Doctor'
        DATA_SCIENTIST = 'data_scientist', 'Data Scientist'
        HOSPITAL_ADMIN = 'hospital_admin', 'Hospital Admin'
        ADMIN          = 'admin',          'Admin'

    role = models.CharField(max_length=20, choices=Role.choices, default=Role.PATIENT)
    profile_picture = models.ImageField(upload_to='profile_pics/', blank=True, null=True)
    phone_number = models.CharField(max_length=20, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    is_approved = models.BooleanField(default=True,
        help_text='Patients are auto-approved. Doctors, Data Scientists, and Hospital Admins require admin approval.')

    def __str__(self):
        return f'{self.username} ({self.get_role_display()})'

    @property
    def is_patient(self):        return self.role == self.Role.PATIENT
    @property
    def is_doctor(self):         return self.role == self.Role.DOCTOR
    @property
    def is_data_scientist(self): return self.role == self.Role.DATA_SCIENTIST
    @property
    def is_hospital_admin(self): return self.role == self.Role.HOSPITAL_ADMIN


class PatientProfile(models.Model):
    user = models.OneToOneField(CustomUser, on_delete=models.CASCADE, related_name='patient_profile')
    blood_type = models.CharField(max_length=5, blank=True,
        choices=[('A+','A+'),('A-','A-'),('B+','B+'),('B-','B-'),
                 ('AB+','AB+'),('AB-','AB-'),('O+','O+'),('O-','O-')])
    allergies = models.TextField(blank=True)
    emergency_contact_name = models.CharField(max_length=100, blank=True)
    emergency_contact_phone = models.CharField(max_length=20, blank=True)
    national_id = models.CharField(max_length=50, blank=True)

    def __str__(self): return f'Patient: {self.user.username}'


class DoctorProfile(models.Model):
    user = models.OneToOneField(CustomUser, on_delete=models.CASCADE, related_name='doctor_profile')
    specialty = models.CharField(max_length=100, blank=True)
    license_number = models.CharField(max_length=50, blank=True)
    hospital = models.CharField(max_length=200, blank=True)
    department = models.CharField(max_length=100, blank=True)

    def __str__(self): return f'Dr. {self.user.get_full_name()} - {self.specialty}'


class DataScientistProfile(models.Model):
    user = models.OneToOneField(CustomUser, on_delete=models.CASCADE, related_name='scientist_profile')
    institution = models.CharField(max_length=200, blank=True)
    research_area = models.CharField(max_length=200, blank=True)
    approved_by = models.ForeignKey(CustomUser, null=True, blank=True,
                    on_delete=models.SET_NULL, related_name='approved_scientists')
    approved_at = models.DateTimeField(null=True, blank=True)

    def __str__(self): return f'Scientist: {self.user.username}'


class HospitalAdminProfile(models.Model):
    user = models.OneToOneField(CustomUser, on_delete=models.CASCADE, related_name='hospital_admin_profile')
    hospital_name = models.CharField(max_length=200)
    hospital_code = models.CharField(max_length=50, blank=True)

    def __str__(self): return f'HospAdmin: {self.user.username} @ {self.hospital_name}'


class PatientDoctorRelationship(models.Model):
    patient = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='my_doctors')
    doctor = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='my_patients')
    linked_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL,
                    null=True, blank=True, related_name='relationships_created')
    hospital = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = ['patient', 'doctor']

    def __str__(self): return f'{self.patient.username} <-> Dr. {self.doctor.username}'

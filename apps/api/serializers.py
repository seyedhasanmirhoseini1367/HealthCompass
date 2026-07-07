from rest_framework import serializers
from apps.accounts.models import CustomUser
from apps.medical_records.models import MedicalRecord


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model  = CustomUser
        fields = ['id', 'username', 'email', 'role', 'is_approved', 'profile_picture']
        read_only_fields = fields


class RegisterSerializer(serializers.ModelSerializer):
    password  = serializers.CharField(write_only=True, min_length=8)
    password2 = serializers.CharField(write_only=True)

    class Meta:
        model  = CustomUser
        fields = ['email', 'password', 'password2', 'role']

    def validate(self, data):
        if data['password'] != data['password2']:
            raise serializers.ValidationError({'password': 'Passwords do not match.'})
        return data

    def create(self, validated_data):
        validated_data.pop('password2')
        user = CustomUser.objects.create_user(
            username=validated_data['email'],
            email=validated_data['email'],
            password=validated_data['password'],
            role=validated_data.get('role', 'patient'),
        )
        return user


class MedicalRecordSerializer(serializers.ModelSerializer):
    record_type_display = serializers.CharField(source='get_record_type_display', read_only=True)

    class Meta:
        model  = MedicalRecord
        fields = [
            'id', 'title', 'record_type', 'record_type_display',
            'record_date', 'uploaded_at', 'is_flagged', 'notes',
            'parsed_data',
        ]
        read_only_fields = ['uploaded_at', 'is_flagged']

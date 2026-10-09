from django.contrib.auth import get_user_model
from rest_framework import serializers


class UserRegistrationSerializer(serializers.ModelSerializer):
    class Meta:
        model = get_user_model()
        fields = ('email', 'first_name', 'last_name', 'password')
        extra_kwargs = {
            'password': {'write_only': True, 'trim_whitespace': False},
            # Uniqueness is checked after manager normalization in validate_email.
            'email': {'validators': []},
        }

    def validate_email(self, value):
        email = self.Meta.model.objects.normalize_email(value)
        if self.Meta.model.objects.filter(email=email).exists():
            raise serializers.ValidationError(
                'A user with this email already exists.', code='unique',
            )
        return email

    def create(self, validated_data):
        return self.Meta.model.objects.create_user(**validated_data)


class UserProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = get_user_model()
        fields = ('id', 'email', 'first_name', 'last_name', 'is_staff')
        read_only_fields = ('id', 'is_staff')
        extra_kwargs = {'email': {'validators': []}}

    def validate_email(self, value):
        email = self.Meta.model.objects.normalize_email(value)
        users = self.Meta.model.objects.filter(email=email)
        if self.instance is not None:
            users = users.exclude(pk=self.instance.pk)
        if users.exists():
            raise serializers.ValidationError(
                'A user with this email already exists.', code='unique',
            )
        return email

from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User
from django.db import transaction

from .models import Appointment, Department, Doctor, Patient


class DepartmentForm(forms.ModelForm):
    class Meta:
        model = Department
        fields = ['name', 'description']
        widgets = {
            'description': forms.Textarea(attrs={'rows': 3}),
        }


class DoctorForm(forms.ModelForm):
    class Meta:
        model = Doctor
        fields = ['name', 'specialty', 'email', 'department']


class PatientForm(forms.ModelForm):
    class Meta:
        model = Patient
        fields = ['name', 'date_of_birth', 'phone']
        widgets = {
            'date_of_birth': forms.DateInput(attrs={'type': 'date'}),
        }


class AppointmentForm(forms.ModelForm):
    class Meta:
        model = Appointment
        fields = ['patient', 'doctor', 'date_time', 'reason', 'status']
        widgets = {
            'date_time': forms.DateTimeInput(attrs={'type': 'datetime-local'}),
            'reason': forms.Textarea(attrs={'rows': 3}),
        }


class PatientRegistrationForm(UserCreationForm):
    """Public-facing patient self-registration. Creates a User and a linked Patient."""

    full_name     = forms.CharField(label='Full name', max_length=150)
    date_of_birth = forms.DateField(
        label='Date of birth',
        widget=forms.DateInput(attrs={
            'type': 'date',
            'class': 'input--lg',
            'autocomplete': 'bday',
        }),
        help_text='Used to compute your age for clinical context.',
    )
    phone         = forms.CharField(label='Phone', max_length=20)
    email         = forms.EmailField(label='Email')

    class Meta:
        model = User
        fields = ['username', 'full_name', 'date_of_birth', 'phone', 'email',
                  'password1', 'password2']

    def clean_email(self):
        email = (self.cleaned_data.get('email') or '').strip().lower()
        if Patient.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError('A patient is already registered with this email.')
        return email

    @transaction.atomic
    def save(self, commit: bool = True) -> User:
        user = super().save(commit=False)
        user.is_staff = False
        user.email = self.cleaned_data['email']
        if commit:
            user.save()
            Patient.objects.create(
                user=user,
                name=self.cleaned_data['full_name'],
                date_of_birth=self.cleaned_data['date_of_birth'],
                phone=self.cleaned_data['phone'],
                email=self.cleaned_data['email'],
            )
        return user


class PatientProfileForm(forms.ModelForm):
    """Lets a logged-in patient edit their own contact + DOB."""

    class Meta:
        model = Patient
        fields = ['name', 'date_of_birth', 'phone', 'email']
        widgets = {
            'date_of_birth': forms.DateInput(attrs={
                'type': 'date',
                'class': 'input--lg',
                'autocomplete': 'bday',
            }),
        }

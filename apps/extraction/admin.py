"""Django admin interface registrations for extraction models."""
from django.contrib import admin

from apps.extraction.models import ExtractedRecord, ExtractionJob


class ExtractedRecordInline(admin.TabularInline):
    model = ExtractedRecord
    extra = 0
    readonly_fields = ('id', 'id_from_service', 'email', 'first_name', 'last_name', 'extracted_at')
    can_delete = False
    show_change_link = True


@admin.register(ExtractionJob)
class ExtractionJobAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'status',
        'source_type',
        'record_count',
        'masked_token',
        'start_time',
        'end_time',
        'created_at',
    )
    list_filter = ('status', 'source_type', 'created_at')
    search_fields = ('id', 'token_hash', 'masked_token', 'error_message')
    readonly_fields = (
        'id',
        'token_hash',
        'masked_token',
        'created_at',
        'updated_at',
        'duration_seconds',
        'progress_percentage',
    )
    inlines = [ExtractedRecordInline]
    ordering = ('-created_at',)


@admin.register(ExtractedRecord)
class ExtractedRecordAdmin(admin.ModelAdmin):
    list_display = ('id_from_service', 'job', 'email', 'first_name', 'last_name', 'extracted_at')
    list_filter = ('extracted_at', 'job__status', 'job__source_type')
    search_fields = ('id_from_service', 'email', 'first_name', 'last_name')
    readonly_fields = ('id', 'job', 'extracted_at')
    ordering = ('-extracted_at',)

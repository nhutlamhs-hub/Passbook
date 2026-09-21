from decimal import Decimal, InvalidOperation
from contextlib import nullcontext
import logging
from time import perf_counter

from django.conf import settings
from django.db import connection
from django.db import transaction
from django.db.models import Prefetch, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import serializers
from rest_framework.views import APIView

from .models import Book, BookImage, Favorite
from .pagination import BookPagination
from .serializers import BookImageSerializer, BookSerializer, BookWriteSerializer, FavoriteSerializer


logger = logging.getLogger(__name__)


class _BookQueryTiming:
    def __init__(self):
        self.count_ms = 0.0
        self.data_ms = 0.0
        self.query_count = 0

    def execute(self, execute, sql, params, many, context):
        started = perf_counter()
        try:
            return execute(sql, params, many, context)
        finally:
            duration_ms = (perf_counter() - started) * 1000
            self.query_count += 1
            if 'COUNT(' in sql.upper():
                self.count_ms += duration_ms
            else:
                self.data_ms += duration_ms


def optimized_books_queryset():
    return (
        Book.objects
        .select_related('seller', 'subject', 'category', 'pickup_location')
        .prefetch_related(
            Prefetch('images', queryset=BookImage.objects.order_by('sort_order', 'id')),
        )
    )


class BookListView(APIView):
    def get(self, request):
        started = perf_counter()
        queryset = optimized_books_queryset().filter(status='available')
        query_params = request.query_params

        search = query_params.get('search')
        if search:
            queryset = queryset.filter(
                Q(title__icontains=search)
                | Q(description__icontains=search)
                | Q(subject__name__icontains=search)
                | Q(subject__code__icontains=search)
                | Q(category__name__icontains=search)
            )

        queryset = self._filter_integer(queryset, query_params, 'subject_id', 'subject_id')
        queryset = self._filter_integer(queryset, query_params, 'category_id', 'category_id')
        queryset = self._filter_integer(queryset, query_params, 'pickup_location_id', 'pickup_location_id')
        queryset = self._filter_integer(queryset, query_params, 'university_id', 'seller__university_id')

        condition = query_params.get('condition_status')
        if condition:
            valid_conditions = {value for value, _ in Book.CONDITION_CHOICES}
            if condition not in valid_conditions:
                raise serializers.ValidationError({'condition_status': 'Tình trạng không hợp lệ.'})
            queryset = queryset.filter(condition_status=condition)

        min_price = self._parse_price(query_params, 'min_price')
        max_price = self._parse_price(query_params, 'max_price')
        if min_price is not None:
            queryset = queryset.filter(price__gte=min_price)
        if max_price is not None:
            queryset = queryset.filter(price__lte=max_price)
        if min_price is not None and max_price is not None and min_price > max_price:
            raise serializers.ValidationError({'price': 'min_price không được lớn hơn max_price.'})

        sort_options = {
            'newest': '-created_at',
            'oldest': 'created_at',
            'price_asc': 'price',
            'price_desc': '-price',
        }
        sort = query_params.get('sort', 'newest')
        if sort not in sort_options:
            raise serializers.ValidationError({'sort': 'Giá trị sort không hợp lệ.'})
        queryset = queryset.order_by(sort_options[sort], '-id')

        paginator = BookPagination()
        timing = _BookQueryTiming()
        with connection.execute_wrapper(timing) if settings.BOOKS_TIMING_ENABLED else nullcontext():
            pagination_started = perf_counter()
            page = paginator.paginate_queryset(queryset, request, view=self)
            books = list(page)
            pagination_ms = (perf_counter() - pagination_started) * 1000
            serialization_started = perf_counter()
            serialized = BookSerializer(books, many=True).data
            serialization_ms = (perf_counter() - serialization_started) * 1000

        if settings.BOOKS_TIMING_ENABLED:
            logger.info(
                'books_timing path=%s count_ms=%.2f data_sql_ms=%.2f '
                'query_count=%d pagination_ms=%.2f serialization_ms=%.2f total_ms=%.2f',
                request.get_full_path(),
                timing.count_ms,
                timing.data_ms,
                timing.query_count,
                pagination_ms,
                serialization_ms,
                (perf_counter() - started) * 1000,
            )
        return paginator.get_paginated_response(serialized)

    @staticmethod
    def _filter_integer(queryset, query_params, parameter, lookup):
        value = query_params.get(parameter)
        if value is None:
            return queryset
        try:
            parsed = int(value)
        except ValueError as exc:
            raise serializers.ValidationError({parameter: f'{parameter} phải là số nguyên.'}) from exc
        if parsed < 1:
            raise serializers.ValidationError({parameter: f'{parameter} phải lớn hơn 0.'})
        return queryset.filter(**{lookup: parsed})

    @staticmethod
    def _parse_price(query_params, parameter):
        value = query_params.get(parameter)
        if value is None:
            return None
        try:
            parsed = Decimal(value)
        except (InvalidOperation, ValueError) as exc:
            raise serializers.ValidationError({parameter: f'{parameter} không hợp lệ.'}) from exc
        if parsed < 0:
            raise serializers.ValidationError({parameter: f'{parameter} không được âm.'})
        return parsed

    def post(self, request):
        if not request.user.is_authenticated:
            self.permission_denied(request)
        serializer = BookWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            book = serializer.save(
            seller=request.user,
            status='available',
            created_at=timezone.now(),
            updated_at=timezone.now(),
            )
        book = optimized_books_queryset().get(pk=book.pk)
        return Response(BookSerializer(book).data, status=status.HTTP_201_CREATED)


class MyBooksView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        queryset = optimized_books_queryset().filter(seller_id=request.user.id)
        query_params = request.query_params

        listing_status = query_params.get('status')
        if listing_status:
            valid_statuses = {value for value, _ in Book.STATUS_CHOICES}
            if listing_status not in valid_statuses:
                raise serializers.ValidationError({'status': 'Trạng thái không hợp lệ.'})
            queryset = queryset.filter(status=listing_status)

        search = query_params.get('search')
        if search:
            queryset = queryset.filter(
                Q(title__icontains=search)
                | Q(description__icontains=search)
                | Q(subject__name__icontains=search)
                | Q(subject__code__icontains=search)
                | Q(category__name__icontains=search)
            ).distinct()

        sort_options = {
            'newest': '-created_at',
            'oldest': 'created_at',
            'price_asc': 'price',
            'price_desc': '-price',
        }
        sort = query_params.get('sort', 'newest')
        if sort not in sort_options:
            raise serializers.ValidationError({'sort': 'Giá trị sort không hợp lệ.'})
        queryset = queryset.order_by(sort_options[sort], '-id')

        paginator = BookPagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        return paginator.get_paginated_response(BookSerializer(page, many=True).data)


class BookDetailView(APIView):
    def get(self, request, pk):
        book = get_object_or_404(
            optimized_books_queryset().filter(status='available'),
            pk=pk,
        )
        return Response(BookSerializer(book).data)

    def patch(self, request, pk):
        self._require_owner(request, pk)
        book = get_object_or_404(optimized_books_queryset(), pk=pk)
        serializer = BookWriteSerializer(book, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            book = serializer.save(updated_at=timezone.now())
        book = optimized_books_queryset().get(pk=book.pk)
        return Response(BookSerializer(book).data)

    def delete(self, request, pk):
        self._require_owner(request, pk)
        book = get_object_or_404(Book, pk=pk)
        book.status = 'deleted'
        book.updated_at = timezone.now()
        book.save(update_fields=['status', 'updated_at'])
        return Response({'message': 'Đã xóa tin đăng.'})

    def _require_owner(self, request, pk):
        if not request.user.is_authenticated:
            self.permission_denied(request)
        book = get_object_or_404(Book, pk=pk)
        if book.seller_id != request.user.id:
            self.permission_denied(request, message='Bạn không có quyền chỉnh sửa tin đăng này.')

class BookSoldView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request, pk):
        book = get_object_or_404(Book, pk=pk)
        if book.seller_id != request.user.id:
            self.permission_denied(request, message='Bạn không có quyền cập nhật tin đăng này.')
        book.status = 'sold'
        book.updated_at = timezone.now()
        book.save(update_fields=['status', 'updated_at'])
        book = optimized_books_queryset().get(pk=book.pk)
        return Response(BookSerializer(book).data)


class BookImageListView(APIView):
    def get(self, request, book_id):
        book = get_object_or_404(Book, pk=book_id, status='available')
        images = BookImage.objects.filter(book=book).order_by('sort_order', 'id')
        return Response({
            'book_id': book.id,
            'images': BookImageSerializer(images, many=True).data,
        })

    def post(self, request, book_id):
        book = self._get_editable_book(request, book_id)
        serializer = BookImageSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            if serializer.validated_data.get('is_primary', False):
                BookImage.objects.filter(book=book, is_primary=True).update(is_primary=False)
            image = serializer.save(book=book, created_at=timezone.now())
        return Response(BookImageSerializer(image).data, status=status.HTTP_201_CREATED)

    @staticmethod
    def _get_editable_book(request, book_id):
        if not request.user.is_authenticated:
            from rest_framework.exceptions import NotAuthenticated
            raise NotAuthenticated()
        book = get_object_or_404(Book, pk=book_id)
        if book.status != 'available':
            from rest_framework.exceptions import ValidationError
            raise ValidationError({'book': 'Chỉ có thể chỉnh sửa ảnh của sách đang được đăng bán.'})
        if book.seller_id != request.user.id:
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied('Bạn không có quyền chỉnh sửa ảnh của tin đăng này.')
        return book


class BookImageDetailView(APIView):
    def patch(self, request, book_id, image_id):
        book = BookImageListView._get_editable_book(request, book_id)
        image = get_object_or_404(BookImage, pk=image_id, book=book)
        serializer = BookImageSerializer(image, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            if serializer.validated_data.get('is_primary') is True:
                BookImage.objects.filter(book=book, is_primary=True).exclude(pk=image.pk).update(is_primary=False)
            image = serializer.save()
        return Response(BookImageSerializer(image).data)

    def delete(self, request, book_id, image_id):
        book = BookImageListView._get_editable_book(request, book_id)
        image = get_object_or_404(BookImage, pk=image_id, book=book)
        image.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class FavoriteCreateDeleteView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, book_id):
        book = get_object_or_404(Book, pk=book_id, status='available')
        favorite, created = Favorite.objects.get_or_create(
            user=request.user,
            book=book,
            defaults={'created_at': timezone.now()},
        )
        return Response(
            {'book_id': book.id, 'is_favorite': True},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    def delete(self, request, book_id):
        favorite = get_object_or_404(Favorite, user=request.user, book_id=book_id)
        favorite.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class FavoriteListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        queryset = (
            Favorite.objects
            .filter(user=request.user, book__status='available')
            .select_related('book', 'book__seller')
            .prefetch_related(
                Prefetch('book__images', queryset=BookImage.objects.order_by('sort_order', 'id')),
            )
            .order_by('-created_at', '-id')
        )
        paginator = BookPagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        return paginator.get_paginated_response(FavoriteSerializer(page, many=True).data)

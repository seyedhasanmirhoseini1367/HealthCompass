"""
Pagination for the API (audit ARCH-08, PERF-07).

Lists used to be unpaginated or cut at a hard 20-100 with no way to fetch the
rest. Every list is now paged (PAGE_SIZE 20, ?page_size up to 100).

Existing clients parse these endpoints as bare JSON arrays, and response shapes
must not change, so a page is still a bare array. The page metadata travels in
headers: X-Total-Count, and an RFC 8288 Link header with rel="next"/"prev".
"""
from django.core.paginator import EmptyPage, PageNotAnInteger
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response


class StandardPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100


def paginated(request, queryset, serializer_class, *, context=None):
    """Response with one page of *queryset* as a bare list plus paging headers."""
    paginator = StandardPagination()
    try:
        page = paginator.paginate_queryset(queryset, request)
    except (EmptyPage, PageNotAnInteger):
        page = []
    except Exception as exc:  # DRF raises NotFound for an out-of-range page
        if exc.__class__.__name__ != 'NotFound':
            raise
        page = []
    data = serializer_class(page, many=True, context=context or {'request': request}).data
    response = Response(data)
    django_page = getattr(paginator, 'page', None)
    if django_page is not None:
        response['X-Total-Count'] = str(django_page.paginator.count)
        links = []
        if paginator.get_next_link():
            links.append(f'<{paginator.get_next_link()}>; rel="next"')
        if paginator.get_previous_link():
            links.append(f'<{paginator.get_previous_link()}>; rel="prev"')
        if links:
            response['Link'] = ', '.join(links)
    return response

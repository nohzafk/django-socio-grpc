import asyncio
import logging

from asgiref.sync import async_to_sync, sync_to_async
from django.core.exceptions import ValidationError
from django.db.models.query import QuerySet
from django.http import Http404
from django.shortcuts import get_object_or_404
from rest_framework.filters import BaseFilterBackend
from rest_framework.pagination import BasePagination

from django_socio_grpc import mixins, services
from django_socio_grpc.exceptions import NotFound
from django_socio_grpc.proto_serializers import ProtoSerializer
from django_socio_grpc.settings import grpc_settings
from django_socio_grpc.utils import model_meta
from django_socio_grpc.utils.tools import rreplace

logger = logging.getLogger("django_socio_grpc.services")


class GenericService(services.Service):
    """
    Base class for all other generic services.
    """

    # Either set this attribute or override ``get_queryset()``.
    queryset: QuerySet | None = None
    # Either set this attribute or override ``get_serializer_class()``.
    serializer_class: ProtoSerializer | None = None
    # Set this if you want to use object lookups other than id
    lookup_field: str | None = None
    lookup_request_field: str | None = None
    # The filter backend classes to use for queryset filtering
    filter_backends: list[BaseFilterBackend] = grpc_settings.DEFAULT_FILTER_BACKENDS

    # The style to use for queryset pagination.
    pagination_class: BasePagination | None = grpc_settings.DEFAULT_PAGINATION_CLASS

    service_name: str | None = None

    @classmethod
    def get_service_name(cls):
        if cls.service_name:
            return cls.service_name
        else:
            return rreplace(cls.__name__, "Service", "", 1)

    def get_queryset(self):
        """
        Get the list of items for this service.
        This must be an iterable, and may be a queryset.
        Defaults to using ``self.queryset``.

        If you are overriding a handler method, it is important that you call
        ``get_queryset()`` instead of accessing the ``queryset`` attribute as
        ``queryset`` will get evaluated only once.

        Override this to provide dynamic behavior, for example::

            def get_queryset(self):
                if self.action == 'ListSpecialUser':
                    return SpecialUser.objects.all()
                return super().get_queryset()
        """
        assert self.queryset is not None, (
            "'%s' should either include a ``queryset`` attribute, "
            "or override the ``get_queryset()`` method." % self.__class__.__name__
        )
        queryset = self.queryset
        if isinstance(queryset, QuerySet):
            # Ensure queryset is re-evaluated on each request.
            queryset = queryset.all()
        return queryset

    def get_serializer_class(self):
        """
        Return the class to use for the serializer. Defaults to using
        `self.serializer_class`.
        """
        assert self.serializer_class is not None, (
            "'%s' should either include a `serializer_class` attribute, "
            "or override the `get_serializer_class()` method." % self.__class__.__name__
        )
        return self.serializer_class

    def get_lookup_request_field(self, queryset=None):
        if queryset is None:
            queryset = self.get_queryset()
        lookup_field = self.lookup_field or model_meta.get_model_pk(queryset.model).name
        lookup_request_field = self.lookup_request_field or lookup_field
        return lookup_request_field

    def get_object(self):
        """
        Returns an object instance that should be used for detail services.
        Defaults to using the lookup_field parameter to filter the base
        queryset.
        """
        queryset = self.filter_queryset(self.get_queryset())
        lookup_request_field = self.get_lookup_request_field(queryset)
        assert hasattr(self.request, lookup_request_field), (
            f"Expected service {self.__class__.__name__} to be called with request that has a field "
            f'named "{lookup_request_field}". Fix your request protocol definition, or set the '
            "`.lookup_field` attribute on the service correctly."
        )
        lookup_value = getattr(self.request, lookup_request_field)
        filter_kwargs = {lookup_request_field: lookup_value}
        try:
            obj = get_object_or_404(queryset, **filter_kwargs)
        except (TypeError, ValueError, ValidationError, Http404) as e:
            raise NotFound(
                detail=f"{queryset.model.__name__}: {lookup_value} not found!"
            ) from e
        self.check_object_permissions(obj)
        return obj

    async def aget_object(self):
        """
        Returns an object instance that should be used for detail services.
        Defaults to using the lookup_field parameter to filter the base
        queryset.
        """
        queryset = await sync_to_async(self.get_queryset)()
        queryset = await self.afilter_queryset(queryset)
        lookup_request_field = self.get_lookup_request_field(queryset)
        assert hasattr(self.request, lookup_request_field), (
            f"Expected service {self.__class__.__name__} to be called with request that has a field "
            f'named "{lookup_request_field}". Fix your request protocol definition, or set the '
            "`.lookup_field` attribute on the service correctly."
        )
        lookup_value = getattr(self.request, lookup_request_field)
        filter_kwargs = {lookup_request_field: lookup_value}
        try:
            obj = await sync_to_async(get_object_or_404)(queryset, **filter_kwargs)
        except (TypeError, ValueError, ValidationError, Http404) as e:
            raise NotFound(
                detail=f"{queryset.model.__name__}: {lookup_value} not found!"
            ) from e
        await self.acheck_object_permissions(obj)
        return obj

    def get_serializer(self, *args, **kwargs):
        """
        Return the serializer instance that should be used for validating and
        deserializing input, and for serializing output.
        """
        serializer_class = self.get_serializer_class()
        kwargs.setdefault("context", self.get_serializer_context())
        return serializer_class(*args, **kwargs)

    async def aget_serializer(self, *args, **kwargs):
        serializer_class = self.get_serializer_class()
        kwargs.setdefault("context", self.get_serializer_context())
        return await sync_to_async(serializer_class)(*args, **kwargs)

    def get_serializer_context(self):
        """
        Extra context provided to the serializer class.  Defaults to including
        ``grpc_request``, ``grpc_context``, and ``service`` keys.
        """
        return {
            "grpc_request": self.request,
            "grpc_context": self.context,
            "service": self,
        }

    def filter_queryset(self, queryset):
        """Given a queryset, filter it, returning a new queryset."""

        # INFO - AM - 05/05/2023 - If user has overriden filter_queryset but we are in async context we put a warning message as it can bring filtering issues
        if type(self).afilter_queryset != GenericService.afilter_queryset:
            logger.warning(
                "You have defined a custom afilter_queryset method but you are using sync mixins. Sync mixin use the method filter_queryset. If you want to keep this filtering logic please rename your method"
            )

        for backend in list(self.filter_backends):
            if asyncio.iscoroutinefunction(backend().filter_queryset):
                queryset = async_to_sync(backend().filter_queryset)(
                    self.context, queryset, self
                )
            else:
                queryset = backend().filter_queryset(self.context, queryset, self)
        return queryset

    async def afilter_queryset(self, queryset):
        """Given a queryset, filter it, returning a new queryset."""

        # INFO - AM - 05/05/2023 - If user has overriden filter_queryset but we are in async context we put a warning message as it can bring filtering issues
        if type(self).filter_queryset != GenericService.filter_queryset:
            logger.warning(
                "You have defined a custom filter_queryset method but you are using async mixins. Async mixin use the method afilter_queryset. If you want to keep this filtering logic please rename your method"
            )

        for backend in list(self.filter_backends):
            if asyncio.iscoroutinefunction(backend().filter_queryset):
                queryset = await backend().filter_queryset(self.context, queryset, self)
            else:
                queryset = await sync_to_async(backend().filter_queryset)(
                    self.context, queryset, self
                )
        return queryset

    @property
    def paginator(self):
        """
        The paginator instance associated with the view, or `None`.
        """
        if not hasattr(self, "_paginator"):
            if self.pagination_class is None:
                self._paginator = None
            else:
                self._paginator = self.pagination_class()
        return self._paginator

    def paginate_queryset(self, queryset):
        """
        Return a single page of results, or `None` if pagination is disabled.
        """
        if self.paginator is None:
            return None
        return self.paginator.paginate_queryset(queryset, self.context, view=self)


############################################################
#   Synchronous Service                                    #
############################################################


class CreateService(mixins.CreateModelMixin, GenericService):
    """
    Concrete service for creating a model instance that provides a ``Create()``
    handler.
    """


class ListService(mixins.ListModelMixin, GenericService):
    """
    Concrete service for listing a queryset that provides a ``List()`` handler.
    """


class StreamService(mixins.StreamModelMixin, GenericService):
    """
    Concrete service for listing one by one on streaming a queryset that provides a ``Stream()`` handler.
    """


class RetrieveService(mixins.RetrieveModelMixin, GenericService):
    """
    Concrete service for retrieving a model instance that provides a
    ``Retrieve()`` handler.
    """


class DestroyService(mixins.DestroyModelMixin, GenericService):
    """
    Concrete service for deleting a model instance that provides a ``Destroy()``
    handler.
    """


class UpdateService(mixins.UpdateModelMixin, GenericService):
    """
    Concrete service for updating a model instance that provides a
    ``Update()`` handler.
    """


class ListCreateService(mixins.ListModelMixin, mixins.CreateModelMixin, GenericService):
    """
    Concrete service for listing a queryset that provides a ``List()`` and ``Create()`` handler.
    """


class ReadOnlyModelService(mixins.RetrieveModelMixin, mixins.ListModelMixin, GenericService):
    """
    Concrete service that provides default ``List()`` and ``Retrieve()``
    handlers.
    """


class ModelService(
    mixins.CreateModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    mixins.ListModelMixin,
    mixins.PartialUpdateModelMixin,
    GenericService,
):
    """
    Concrete service that provides default ``Create()``, ``Retrieve()``,
    ``Update()``, ``Destroy()`` and ``List()`` handlers.
    """


############################################################
#   Asynchronous Services                                  #
############################################################
class AsyncCreateService(mixins.AsyncCreateModelMixin, GenericService):
    """
    Concrete service for creating a model instance that provides a ``Create()``
    handler.
    """


class AsyncListService(mixins.AsyncListModelMixin, GenericService):
    """
    Concrete service for listing a queryset that provides a ``List()`` handler.
    """


class AsyncStreamService(mixins.AsyncStreamModelMixin, GenericService):
    """
    Concrete service for listing one by one on streaming a queryset that provides a ``Stream()`` handler.
    """


class AsyncRetrieveService(mixins.AsyncRetrieveModelMixin, GenericService):
    """
    Concrete service for retrieving a model instance that provides a
    ``Retrieve()`` handler.
    """


class AsyncDestroyService(mixins.AsyncDestroyModelMixin, GenericService):
    """
    Concrete service for deleting a model instance that provides a ``Destroy()``
    handler.
    """


class AsyncUpdateService(mixins.AsyncUpdateModelMixin, GenericService):
    """
    Concrete service for updating a model instance that provides a
    ``Update()`` handler.
    """


class AsyncListCreateService(
    mixins.AsyncListModelMixin, mixins.AsyncCreateModelMixin, GenericService
):
    """
    Concrete service for listing a queryset that provides a ``List()`` and ``Create()`` handler.
    """


class AsyncReadOnlyModelService(
    mixins.AsyncRetrieveModelMixin, mixins.AsyncListModelMixin, GenericService
):
    """
    Concrete service that provides default ``List()`` and ``Retrieve()``
    handlers.
    """


class AsyncModelService(
    mixins.AsyncCreateModelMixin,
    mixins.AsyncRetrieveModelMixin,
    mixins.AsyncUpdateModelMixin,
    mixins.AsyncDestroyModelMixin,
    mixins.AsyncListModelMixin,
    mixins.AsyncPartialUpdateModelMixin,
    GenericService,
):
    """
    Concrete service that provides default ``Create()``, ``Retrieve()``,
    ``Update()``, ``Destroy()`` and ``List()`` handlers.
    """

from __future__ import annotations

import re
from collections import defaultdict
from enum import Enum
from pathlib import Path
from typing import (
    Any,
    Callable,
    ClassVar,
    DefaultDict,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Pattern,
    Sequence,
    Set,
    Tuple,
    Type,
    TypeVar,
    Union,
)
from urllib.parse import ParseResult

from pydantic import Field

from datamodel_code_generator import (
    DefaultPutDict,
    Error,
    LiteralType,
    OpenAPIScope,
    PythonVersion,
    load_yaml,
    snooper_to_methods,
)
from datamodel_code_generator.model import DataModel, DataModelFieldBase
from datamodel_code_generator.model import pydantic as pydantic_model
from datamodel_code_generator.parser.jsonschema import (
    JsonSchemaObject,
    JsonSchemaParser,
    get_model_by_path,
    get_special_path,
)
from datamodel_code_generator.reference import snake_to_upper_camel
from datamodel_code_generator.types import (
    DataType,
    DataTypeManager,
    EmptyDataType,
    StrictTypes,
)
from datamodel_code_generator.util import BaseModel

RE_APPLICATION_JSON_PATTERN: Pattern[str] = re.compile(r'^application/.*json$')

OPERATION_NAMES: List[str] = [
    'get',
    'put',
    'post',
    'delete',
    'patch',
    'head',
    'options',
    'trace',
]


class ParameterLocation(Enum):
    query = 'query'
    header = 'header'
    path = 'path'
    cookie = 'cookie'


BaseModelT = TypeVar('BaseModelT', bound=BaseModel)


class ReferenceObject(BaseModel):
    ref: str = Field(..., alias='$ref')


class ExampleObject(BaseModel):
    summary: Optional[str] = None
    description: Optional[str] = None
    value: Any = None
    externalValue: Optional[str] = None


class MediaObject(BaseModel):
    schema_: Union[ReferenceObject, JsonSchemaObject, None] = Field(
        None, alias='schema'
    )
    example: Any = None
    examples: Union[str, ReferenceObject, ExampleObject, None] = None


class ParameterObject(BaseModel):
    name: Optional[str] = None
    in_: Optional[ParameterLocation] = Field(None, alias='in')
    description: Optional[str] = None
    required: bool = False
    deprecated: bool = False
    schema_: Optional[JsonSchemaObject] = Field(None, alias='schema')
    example: Any = None
    examples: Union[str, ReferenceObject, ExampleObject, None] = None
    content: Dict[str, MediaObject] = {}


class HeaderObject(BaseModel):
    description: Optional[str] = None
    required: bool = False
    deprecated: bool = False
    schema_: Optional[JsonSchemaObject] = Field(None, alias='schema')
    example: Any = None
    examples: Union[str, ReferenceObject, ExampleObject, None] = None
    content: Dict[str, MediaObject] = {}


class RequestBodyObject(BaseModel):
    description: Optional[str] = None
    content: Dict[str, MediaObject] = {}
    required: bool = False


class ResponseObject(BaseModel):
    description: Optional[str] = None
    headers: Dict[str, ParameterObject] = {}
    content: Dict[Union[str, int], MediaObject] = {}


class Operation(BaseModel):
    tags: List[str] = []
    summary: Optional[str] = None
    description: Optional[str] = None
    operationId: Optional[str] = None
    parameters: List[Union[ReferenceObject, ParameterObject]] = []
    requestBody: Union[ReferenceObject, RequestBodyObject, None] = None
    responses: Dict[Union[str, int], Union[ReferenceObject, ResponseObject]] = {}
    deprecated: bool = False


class ComponentsObject(BaseModel):
    schemas: Dict[str, Union[ReferenceObject, JsonSchemaObject]] = {}
    responses: Dict[str, Union[ReferenceObject, ResponseObject]] = {}
    examples: Dict[str, Union[ReferenceObject, ExampleObject]] = {}
    requestBodies: Dict[str, Union[ReferenceObject, RequestBodyObject]] = {}
    headers: Dict[str, Union[ReferenceObject, HeaderObject]] = {}


@snooper_to_methods(max_variable_length=None)
class OpenAPIParser(JsonSchemaParser):
    SCHEMA_PATH: ClassVar[str] = '#/components/schemas'

    def __init__(
        self,
        source: Union[str, Path, List[Path], ParseResult],
        *,
        openapi_scopes: Optional[List[OpenAPIScope]] = None,
        **ka
    ):
        super().__init__(
            source=source,
            **ka
        )
        self.openapi_scopes: List[OpenAPIScope] = openapi_scopes or [
            OpenAPIScope.Schemas
        ]

    def get_ref_model(self, ref: str) -> Dict[str, Any]:
        ref_file, ref_path = self.model_resolver.resolve_ref(ref).split('#', 1)
        if ref_file:
            ref_body = self._get_ref_body(ref_file)
        else:  # pragma: no cover
            ref_body = self.raw_obj
        return get_model_by_path(ref_body, ref_path.split('/')[1:])

    def resolve_object(
        self, obj: Union[ReferenceObject, BaseModelT], object_type: Type[BaseModelT]
    ) -> BaseModelT:
        if isinstance(obj, ReferenceObject):
            ref_obj = self.get_ref_model(obj.ref)
            return object_type.parse_obj(ref_obj)
        return obj

    def parse_schema(
        self,
        name: str,
        obj: JsonSchemaObject,
        path: List[str],
    ) -> DataType:
        if obj.is_array:
            data_type = self.parse_array(name, obj, [*path, name])
        elif obj.allOf:  # pragma: no cover
            data_type = self.parse_all_of(name, obj, path)
        elif obj.oneOf or obj.anyOf:  # pragma: no cover
            data_type = self.parse_root_type(name, obj, path)
            if isinstance(data_type, EmptyDataType) and obj.properties:
                self.parse_object(name, obj, path)
        elif obj.is_object:
            data_type = self.parse_object(name, obj, path)
        elif obj.enum:  # pragma: no cover
            data_type = self.parse_enum(name, obj, path)
        elif obj.ref:  # pragma: no cover
            data_type = self.get_ref_data_type(obj.ref)
        else:
            data_type = self.get_data_type(obj)
        self.parse_ref(obj, path)
        return data_type

    def parse_request_body(
        self,
        name: str,
        request_body: RequestBodyObject,
        path: List[str],
    ) -> None:
        for (
            media_type,
            media_obj,
        ) in request_body.content.items():  # type: str, MediaObject
            if isinstance(media_obj.schema_, JsonSchemaObject):
                self.parse_schema(name, media_obj.schema_, [*path, media_type])

    def parse_responses(
        self,
        name: str,
        responses: Dict[Union[str, int], Union[ReferenceObject, ResponseObject]],
        path: List[str],
    ) -> Dict[Union[str, int], Dict[str, DataType]]:
        data_types: DefaultDict[Union[str, int], Dict[str, DataType]] = defaultdict(
            dict
        )
        for status_code, detail in responses.items():
            if isinstance(detail, ReferenceObject):
                if not detail.ref:  # pragma: no cover
                    continue
                ref_model = self.get_ref_model(detail.ref)
                content = {
                    k: MediaObject.parse_obj(v)
                    for k, v in ref_model.get('content', {}).items()
                }
            else:
                content = detail.content

            if self.allow_responses_without_content and not content:
                data_types[status_code]['application/json'] = DataType(type='None')

            for content_type, obj in content.items():
                object_schema = obj.schema_
                if not object_schema:  # pragma: no cover
                    continue
                if isinstance(object_schema, JsonSchemaObject):
                    data_types[status_code][content_type] = self.parse_schema(
                        name, object_schema, [*path, str(status_code), content_type]
                    )
                else:
                    data_types[status_code][content_type] = self.get_ref_data_type(
                        object_schema.ref
                    )

        return data_types

    @classmethod
    def parse_tags(
        cls,
        name: str,
        tags: List[str],
        path: List[str],
    ) -> List[str]:
        return tags

    @classmethod
    def _get_model_name(cls, path_name: str, method: str, suffix: str) -> str:
        camel_path_name = snake_to_upper_camel(path_name.replace('/', '_'))
        return f'{camel_path_name}{method.capitalize()}{suffix}'

    def parse_all_parameters(
        self,
        name: str,
        parameters: List[Union[ReferenceObject, ParameterObject]],
        path: List[str],
    ) -> None:
        fields: List[DataModelFieldBase] = []
        exclude_field_names: Set[str] = set()
        reference = self.model_resolver.add(path, name, class_name=True, unique=True)
        for parameter in parameters:
            parameter = self.resolve_object(parameter, ParameterObject)
            parameter_name = parameter.name
            if not parameter_name or parameter.in_ != ParameterLocation.query:
                continue
            field_name, alias = self.model_resolver.get_valid_field_name_and_alias(
                field_name=parameter_name, excludes=exclude_field_names
            )
            if parameter.schema_:
                fields.append(
                    self.get_object_field(
                        field_name=field_name,
                        field=parameter.schema_,
                        field_type=self.parse_item(
                            field_name, parameter.schema_, [*path, name, parameter_name]
                        ),
                        original_field_name=parameter_name,
                        required=parameter.required,
                        alias=alias,
                    )
                )
            else:
                data_types: List[DataType] = []
                object_schema: Optional[JsonSchemaObject] = None
                for (
                    media_type,
                    media_obj,
                ) in parameter.content.items():
                    if not media_obj.schema_:
                        continue
                    object_schema = self.resolve_object(
                        media_obj.schema_, JsonSchemaObject
                    )
                    data_types.append(
                        self.parse_item(
                            field_name,
                            object_schema,
                            [*path, name, parameter_name, media_type],
                        )
                    )

                if not data_types:
                    continue
                if len(data_types) == 1:
                    data_type = data_types[0]
                else:
                    data_type = self.data_type(data_types=data_types)
                    # multiple data_type parse as non-constraints field
                    object_schema = None
                fields.append(
                    self.data_model_field_type(
                        name=field_name,
                        default=object_schema.default if object_schema else None,
                        data_type=data_type,
                        required=parameter.required,
                        alias=alias,
                        constraints=object_schema.dict()
                        if object_schema and self.is_constraints_field(object_schema)
                        else None,
                        nullable=object_schema.nullable
                        if object_schema
                        and self.strict_nullable
                        and (object_schema.has_default or parameter.required)
                        else None,
                        strip_default_none=self.strip_default_none,
                        extras=self.get_field_extras(object_schema)
                        if object_schema
                        else {},
                        use_annotated=self.use_annotated,
                        use_field_description=self.use_field_description,
                        use_default_kwarg=self.use_default_kwarg,
                        original_name=parameter_name,
                        has_default=object_schema.has_default
                        if object_schema
                        else False,
                    )
                )

        if OpenAPIScope.Parameters in self.openapi_scopes and fields:
            self.results.append(
                self.data_model_type(fields=fields, reference=reference)
            )

    def parse_operation(
        self,
        raw_operation: Dict[str, Any],
        path: List[str],
    ) -> None:
        operation = Operation.parse_obj(raw_operation)
        path_name, method = path[-2:]
        if self.use_operation_id_as_name:
            if not operation.operationId:
                raise Error(
                    f'All operations must have an operationId when --use_operation_id_as_name is set.'
                    f'The following path was missing an operationId: {path_name}'
                )
            path_name = operation.operationId
            method = ''
        self.parse_all_parameters(
            self._get_model_name(path_name, method, suffix='ParametersQuery'),
            operation.parameters,
            [*path, 'parameters'],
        )
        if operation.requestBody:
            if isinstance(operation.requestBody, ReferenceObject):
                ref_model = self.get_ref_model(operation.requestBody.ref)
                request_body = RequestBodyObject.parse_obj(ref_model)
            else:
                request_body = operation.requestBody
            self.parse_request_body(
                name=self._get_model_name(path_name, method, suffix='Request'),
                request_body=request_body,
                path=[*path, 'requestBody'],
            )
        self.parse_responses(
            name=self._get_model_name(path_name, method, suffix='Response'),
            responses=operation.responses,
            path=[*path, 'responses'],
        )
        if OpenAPIScope.Tags in self.openapi_scopes:
            self.parse_tags(
                name=self._get_model_name(path_name, method, suffix='Tags'),
                tags=operation.tags,
                path=[*path, 'tags'],
            )

    def parse_raw(self) -> None:
        for source, path_parts in self._get_context_source_path_parts():
            if self.validation:
                from prance import BaseParser

                BaseParser(
                    spec_string=source.text,
                    backend='openapi-spec-validator',
                    encoding=self.encoding,
                )
            specification: Dict[str, Any] = load_yaml(source.text)
            self.raw_obj = specification
            schemas: Dict[Any, Any] = specification.get('components', {}).get(
                'schemas', {}
            )
            security: Optional[List[Dict[str, List[str]]]] = specification.get(
                'security'
            )
            if OpenAPIScope.Schemas in self.openapi_scopes:
                for (
                    obj_name,
                    raw_obj,
                ) in schemas.items():  # type: str, Dict[Any, Any]
                    self.parse_raw_obj(
                        obj_name,
                        raw_obj,
                        [*path_parts, '#/components', 'schemas', obj_name],
                    )
            if OpenAPIScope.Paths in self.openapi_scopes:
                paths: Dict[str, Dict[str, Any]] = specification.get('paths', {})
                parameters: List[Dict[str, Any]] = [
                    self._get_ref_body(p['$ref']) if '$ref' in p else p
                    for p in paths.get('parameters', [])
                    if isinstance(p, dict)
                ]
                paths_path = [*path_parts, '#/paths']
                for path_name, methods in paths.items():
                    # Resolve path items if applicable
                    if '$ref' in methods:
                        methods = self.get_ref_model(methods['$ref'])
                    paths_parameters = parameters[:]
                    if 'parameters' in methods:
                        paths_parameters.extend(methods['parameters'])
                    relative_path_name = path_name[1:]
                    if relative_path_name:
                        path = [*paths_path, relative_path_name]
                    else:  # pragma: no cover
                        path = get_special_path('root', paths_path)
                    for operation_name, raw_operation in methods.items():
                        if operation_name not in OPERATION_NAMES:
                            continue
                        if paths_parameters:
                            if 'parameters' in raw_operation:  # pragma: no cover
                                raw_operation['parameters'].extend(paths_parameters)
                            else:
                                raw_operation['parameters'] = paths_parameters
                        if security is not None and 'security' not in raw_operation:
                            raw_operation['security'] = security
                        self.parse_operation(
                            raw_operation,
                            [*path, operation_name],
                        )
        if OpenAPIScope.Tags in self.openapi_scopes:
            self.openapi_tags = specification.get( 'tags')


        self._resolve_unparsed_json_pointer()

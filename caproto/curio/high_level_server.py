import logging
import inspect
from collections import (namedtuple, OrderedDict, defaultdict)
from types import MethodType

from .. import (ChannelDouble, ChannelInteger, ChannelString,
                ChannelAlarm)


logger = logging.getLogger(__name__)


class PVSpec(namedtuple('PVSpec',
                        'get put attr name dtype value alarm_group')):
    'PV information specification'
    __slots__ = ()
    default_dtype = int

    def __new__(cls, get=None, put=None, attr=None, name=None, dtype=None,
                value=None, alarm_group=None):
        if dtype is None:
            dtype = (type(value[0]) if value is not None
                     else cls.default_dtype)
        if get is not None:
            assert inspect.iscoroutinefunction(get), 'required async def get'
            # TODO check args
        if put is not None:
            assert inspect.iscoroutinefunction(put), 'required async def put'
            # TODO check args
        return super().__new__(cls, get, put, attr, name, dtype, value,
                               alarm_group)

    def new_names(self, attr=None, name=None):
        if attr is None:
            attr = self.attr
        if name is None:
            name = self.name
        return PVSpec(self.get, self.put, attr, name, self.dtype, self.value,
                      self.alarm_group)


class pvproperty:
    'A property-like descriptor for specifying a PV in a group'

    def __init__(self, get=None, put=None, **spec_kw):
        self.attr_name = None  # to be set later
        self.spec_kw = spec_kw
        self.pvspec = PVSpec(get=get, put=put, **spec_kw)

    def __get__(self, instance, owner):
        if instance is None:
            return self.pvspec
        return instance.attr_pvdb[self.attr_name]

    def __set__(self, instance, value):
        instance.attr_pvdb[self.attr_name] = value

    def __delete__(self, instance):
        del instance.attr_pvdb[self.attr_name]

    def __set_name__(self, name):
        self.attr_name = name
        # update the PV specification with the attribute name
        self.pvspec = self.pvspec.new_names(
            self.attr_name,
            self.pvspec.name
            if self.pvspec.name is not None
            else self.attr_name)

    def putter(self, put):
        # update PVSpec with putter
        self.pvspec = PVSpec(self.pvspec.get, put, *self.pvspec[2:])
        return self

    def __call__(self, get, put=None):
        # handles case where pvproperty(**spec_kw)(getter, putter) is used
        self.pvspec = PVSpec(get, put, **self.spec_kw)
        return self


def expand_macros(pv, macros):
    'Expand a PV name with Python {format-style} macros'
    return pv.format(**macros)


class PVGroupMeta(type):
    'Metaclass that finds all pvproperties'
    @classmethod
    def __prepare__(self, name, bases):
        # keep class dictionary items in order
        return OrderedDict()

    def __new__(metacls, name, bases, dct):
        props = [(attr, value)
                 for attr, value in dct.items()
                 if isinstance(value, pvproperty)
                 ]
        dct['__pvs__'] = pvs = OrderedDict()
        for attr, prop in props:
            if prop.attr_name is None:
                # note: for python < 3.6
                prop.__set_name__(attr)

            pvs[attr] = prop

        return super().__new__(metacls, name, bases, dct)


def channeldata_from_pvspec(group, pvspec):
    'Create a ChannelData instance based on a PVSpec'
    full_pvname = expand_macros(group.prefix + pvspec.name, group.macros)
    value = (pvspec.value
             if pvspec.value is not None
             else group.default_values[pvspec.dtype]
             )

    cls = group.type_map[pvspec.dtype]
    inst = cls(value=value, alarm=group.alarms[pvspec.alarm_group])
    inst.pvspec = pvspec

    # and m-m-monkey-patch away!

    if pvspec.get is not None:
        async def read_wrapper(self, data_type):
            value = await pvspec_get(self)
            if value is not None and value != self.value:
                # update the internal state
                logger.debug('value for %s updated: %r', self.pvspec, value)
                await self.write(value)
            return await self._read(data_type)

        pvspec_get = MethodType(pvspec.get, group)
    else:
        async def read_wrapper(self, data_type):
            value = await group.group_read(self)
            if value is not None:
                # update the internal state
                logger.debug('group read value for %s updated: %r',
                             self.pvspec, value)
                await self.write(value)
            return await self._read(data_type)

    inst.read = MethodType(read_wrapper, inst)

    if pvspec.put is not None:
        pvspec_put = MethodType(pvspec.put, group)

        async def verify_value(self, value):
            logger.debug('verify value wrapper')
            return await pvspec_put(self, value)
    else:
        async def verify_value(self, value):
            logger.debug('verify value wrapper')
            return await group.group_write(self, value)
        # TODO read-only option

    inst.verify_value = MethodType(verify_value, inst)
    return (full_pvname, inst)


class PVGroupBase(metaclass=PVGroupMeta):
    'Base class for a group of PVs'
    type_map = {
        str: ChannelString,
        int: ChannelInteger,
        float: ChannelDouble,
    }

    default_values = {
        str: '-',
        int: 0,
        float: 0.0,
    }

    def __init__(self, prefix, macros=None):
        self.macros = macros if macros is not None else {}
        self.prefix = expand_macros(prefix, macros)
        self.alarms = defaultdict(lambda: ChannelAlarm())
        self.pvdb = OrderedDict(channeldata_from_pvspec(self, pvprop.pvspec)
                                for pvname, pvprop in self.__pvs__.items())

        # attribute -> PV instance mapping for quick access by pvproperty
        self.attr_pvdb = OrderedDict(
            (attr, channeldata)
            for attr, channeldata in zip(self.__pvs__, self.pvdb.values()))

    async def group_read(self, instance):
        'Generic read called for channels without `get` defined'
        logger.debug('no-op group read of %s', instance.pvspec.attr)

    async def group_write(self, instance, value):
        'Generic write called for channels without `put` defined'
        logger.debug('group write of %s = %s', instance.pvspec.attr, value)
        return value

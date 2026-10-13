# SPDX-License-Identifier: Apache-2.0
from django.db import IntegrityError, transaction
from rest_framework import serializers

from orders.models import Charge, Parcel, Shipment


class ChargeSerializer(serializers.ModelSerializer):
    class Meta:
        model = Charge
        fields = ["id", "code", "amount"]
        read_only_fields = ["id"]

    def to_internal_value(self, data):
        if isinstance(data, dict):
            errors = {
                field: ["This field is required."]
                for field in ("code", "amount")
                if field not in data
            }
            if errors:
                raise serializers.ValidationError(errors)
        return super().to_internal_value(data)


class ParcelSerializer(serializers.ModelSerializer):
    quantity = serializers.IntegerField(min_value=1)
    charges = ChargeSerializer(many=True)

    class Meta:
        model = Parcel
        fields = ["id", "label", "quantity", "charges"]
        read_only_fields = ["id"]

    def to_internal_value(self, data):
        if isinstance(data, dict):
            errors = {
                field: ["This field is required."]
                for field in ("label", "quantity", "charges")
                if field not in data
            }
            if errors:
                raise serializers.ValidationError(errors)
        return super().to_internal_value(data)


class ShipmentSerializer(serializers.ModelSerializer):
    parcels = ParcelSerializer(many=True)

    class Meta:
        model = Shipment
        fields = ["id", "reference", "memo", "parcels"]
        read_only_fields = ["id"]

    def create(self, validated_data):
        parcels_data = validated_data.pop("parcels")
        with transaction.atomic():
            shipment = Shipment.objects.create(**validated_data)
            self._create_parcels(shipment, parcels_data)
        return shipment

    def update(self, instance, validated_data):
        parcels_data = validated_data.pop("parcels", serializers.empty)
        with transaction.atomic():
            for name, value in validated_data.items():
                setattr(instance, name, value)
            instance.save()
            if parcels_data is not serializers.empty:
                instance.parcels.all().delete()
                self._create_parcels(instance, parcels_data)
        return instance

    def _create_parcels(self, shipment, parcels_data):
        for parcel_index, parcel_data in enumerate(parcels_data):
            charges_data = parcel_data.pop("charges")
            try:
                parcel = Parcel.objects.create(shipment=shipment, **parcel_data)
            except IntegrityError as exc:
                errors = {
                    str(parcel_index): {
                        "non_field_errors": ["The fields shipment, label must make a unique set."]
                    }
                }
                raise serializers.ValidationError({"parcels": errors}) from exc
            for charge_index, charge_data in enumerate(charges_data):
                try:
                    Charge.objects.create(parcel=parcel, **charge_data)
                except IntegrityError as exc:
                    errors = {
                        str(parcel_index): {
                            "charges": {
                                str(charge_index): {
                                    "non_field_errors": [
                                        "The fields parcel, code must make a unique set."
                                    ]
                                }
                            }
                        }
                    }
                    raise serializers.ValidationError({"parcels": errors}) from exc

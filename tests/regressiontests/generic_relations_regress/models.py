from django.db import models
from django.contrib.contenttypes import generic
from django.contrib.contenttypes.models import ContentType

class Link(models.Model):
    content_type = models.ForeignKey(ContentType)
    object_id = models.PositiveIntegerField()
    content_object = generic.GenericForeignKey()

    def __unicode__(self):
        return "Link to %s id=%s" % (self.content_type, self.object_id)

class Place(models.Model):
    name = models.CharField(max_length=100)
    links = generic.GenericRelation(Link)

    def __unicode__(self):
        return "Place: %s" % self.name

class Restaurant(Place):
    def __unicode__(self):
        return "Restaurant: %s" % self.name

class Address(models.Model):
    street = models.CharField(max_length=80)
    city = models.CharField(max_length=50)
    state = models.CharField(max_length=2)
    zipcode = models.CharField(max_length=5)
    content_type = models.ForeignKey(ContentType)
    object_id = models.PositiveIntegerField()
    content_object = generic.GenericForeignKey()

    def __unicode__(self):
        return '%s %s, %s %s' % (self.street, self.city, self.state, self.zipcode)

class Person(models.Model):
    account = models.IntegerField(primary_key=True)
    name = models.CharField(max_length=128)
    addresses = generic.GenericRelation(Address)

    def __unicode__(self):
        return self.name
